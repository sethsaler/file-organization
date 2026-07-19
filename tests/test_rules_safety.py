from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_manifest import restore_from_manifest
from org_organizer import Organizer
from org_rules import (
    FALLBACK_NEEDS_REVIEW,
    ArchiveRecipe,
    RoutingRule,
    RuleSet,
    append_rule_for_review_choice,
    load_archive_recipe,
    load_rule_set,
    save_rule_set,
)
from org_safety import (
    approve_review_item,
    manifest_for_item,
    original_source_for_review,
    scan_safety_items,
)
from org_watch_status import WatchStatus, read_watch_status
import quick_controls
from schedule_config import FolderJob, ScheduleConfig, build_organize_cmd


def organizer(base: Path, **overrides) -> Organizer:
    options = {
        "base": base,
        "recursive": True,
        "strategy": "flatten-root",
        "include_hidden": True,
        "normalize": "standard",
        "collect_empty_dirs": False,
        "dry_run": False,
        "create_backup": True,
    }
    options.update(overrides)
    return Organizer(**options)


def test_rule_preview_explains_match_and_holds_unmatched(tmp_path: Path) -> None:
    (tmp_path / "invoice-july.pdf").write_bytes(b"pdf")
    (tmp_path / "mystery.bin").write_bytes(b"unknown")
    rules = RuleSet(
        unmatched=FALLBACK_NEEDS_REVIEW,
        rules=[
            RoutingRule(
                id="receipts",
                name="Receipts",
                destination="Documents/Receipts",
                extensions=["pdf"],
                filename_globs=["*invoice*"],
            )
        ],
    )

    summary = organizer(tmp_path, dry_run=True, rule_set=rules).run()

    planned = {move["from"]: move for move in summary["planned_moves"]}
    assert planned["invoice-july.pdf"]["to"] == "Documents/Receipts/invoice-july.pdf"
    assert planned["invoice-july.pdf"]["reason"] == "Rule: Receipts"
    assert planned["mystery.bin"]["to"] == "Needs Review/mystery.bin"
    assert summary["routing"]["matched_by_rule"] == {"Receipts": 1}
    assert summary["routing"]["needs_review_files"] == 1
    assert (tmp_path / "invoice-july.pdf").is_file()


def test_rules_live_move_review_approval_and_learned_rule(tmp_path: Path) -> None:
    source = tmp_path / "incoming" / "clip.xyz"
    source.parent.mkdir()
    source.write_bytes(b"clip")
    rules_path = tmp_path / "rules.json"
    save_rule_set(rules_path, RuleSet(unmatched=FALLBACK_NEEDS_REVIEW))
    rules = load_rule_set(rules_path)

    summary = organizer(tmp_path, rule_set=rules).run()
    review_item = tmp_path / "Needs Review" / "clip.xyz"
    assert review_item.is_file()
    assert original_source_for_review(tmp_path, review_item) == tmp_path / "incoming" / "clip.xyz"

    target, manifest = approve_review_item(tmp_path, review_item, "Videos/Reviewed")
    append_rule_for_review_choice(
        rules_path,
        source=source,
        destination="Videos/Reviewed",
        criterion="Extension",
    )
    assert target == tmp_path / "Videos" / "Reviewed" / "clip.xyz"
    assert manifest and Path(manifest).is_file()
    learned = load_rule_set(rules_path)
    assert learned.rules[-1].extensions == ["xyz"]
    assert learned.rules[-1].destination == "Videos/Reviewed"
    assert summary["backup_manifest"]


def test_archive_recipe_uses_exact_recents_paths_and_external_restore(tmp_path: Path) -> None:
    source = tmp_path / "Downloader"
    archive = tmp_path / "Archive"
    source.mkdir()
    archive.mkdir()
    (source / "loose.jpg").write_bytes(b"image")
    creator = source / "creator-one"
    creator.mkdir()
    (creator / "01.mp4").write_bytes(b"video")
    (creator / "source-url.txt").write_text("https://example.com/post", encoding="utf-8")
    recipe = ArchiveRecipe(
        archive_root=archive,
        folder_mappings={"creator-one": "Manual Library/Creator One"},
    )

    preview = organizer(source, dry_run=True, archive_recipe=recipe).run()
    destinations = {move["from"]: move["to"] for move in preview["planned_moves"]}
    assert destinations["loose.jpg"] == str(archive / "Recents" / "Images" / "loose.jpg")
    assert destinations["creator-one/01.mp4"] == str(archive / "Manual Library" / "Creator One" / "01.mp4")
    assert preview["routing"]["external_moves"] == 3

    summary = organizer(source, archive_recipe=recipe).run()
    assert (archive / "Recents" / "Images" / "loose.jpg").is_file()
    assert (archive / "Manual Library" / "Creator One" / "01.mp4").is_file()
    assert (archive / "Manual Library" / "Creator One" / "source-url.txt").is_file()
    assert summary["archive_backup_manifest"]

    safety_items = scan_safety_items([archive])
    assert safety_items == []
    assert restore_from_manifest(summary["archive_backup_manifest"])
    assert (source / "loose.jpg").is_file()
    assert (source / "creator-one" / "01.mp4").is_file()
    assert (source / "creator-one" / "source-url.txt").is_file()


def test_archive_unknown_creator_is_reviewable_and_recoverable(tmp_path: Path) -> None:
    source = tmp_path / "Downloader"
    archive = tmp_path / "Archive"
    unknown = source / "unknown-creator"
    unknown.mkdir(parents=True)
    archive.mkdir()
    (unknown / "photo.jpg").write_bytes(b"image")

    summary = organizer(source, archive_recipe=ArchiveRecipe(archive_root=archive)).run()
    held = archive / "Needs Review" / "unknown-creator" / "photo.jpg"
    assert held.is_file()
    items = scan_safety_items([archive])
    assert len(items) == 1
    assert items[0].path == archive / "Needs Review" / "unknown-creator"
    assert manifest_for_item(items[0]) == Path(summary["archive_backup_manifest"])


def test_archive_mapping_file_and_schedule_fields_round_trip(tmp_path: Path) -> None:
    archive = tmp_path / "Archive"
    archive.mkdir()
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps({"mappings": {"Alice": "Manual Library/Alice"}}),
        encoding="utf-8",
    )
    recipe = load_archive_recipe(archive, mapping)
    assert recipe.folder_mappings == {"alice": "Manual Library/Alice"}

    job = FolderJob(
        path=str(tmp_path / "Downloader"),
        rules_file=str(tmp_path / "rules.json"),
        unmatched_mode="needs-review",
        archive_root=str(archive),
        archive_mapping=str(mapping),
    )
    cfg = ScheduleConfig.from_json_dict(ScheduleConfig(folders=[job]).to_json_dict())
    loaded = cfg.folders[0]
    assert loaded.rules_file == job.rules_file
    assert loaded.unmatched_mode == "needs-review"
    assert loaded.archive_root == str(archive)
    command = build_organize_cmd(loaded)
    assert "--archive-root" in command
    assert "--archive-mapping" in command
    assert "--rules" not in command  # Archive recipe takes explicit precedence.


def test_archive_recipe_rejects_overlapping_roots_and_escaping_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "Archive"
    source = archive / "Downloader"
    source.mkdir(parents=True)
    with pytest.raises(ValueError, match="must not overlap"):
        ArchiveRecipe(archive_root=archive).validate_source_root(source)

    separate_source = tmp_path / "Separate Downloader"
    separate_source.mkdir()
    outside = tmp_path / "Outside"
    outside.mkdir()
    (archive / "Manual Library").symlink_to(outside, target_is_directory=True)
    recipe = ArchiveRecipe(
        archive_root=archive,
        folder_mappings={"creator": "Manual Library/Creator"},
    )
    media = separate_source / "creator" / "photo.jpg"
    media.parent.mkdir()
    media.write_bytes(b"image")
    with pytest.raises(ValueError, match="escapes"):
        recipe.decide(source_root=separate_source, source=media, bucket="Images")


def test_watch_status_snapshot(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    status = WatchStatus(
        backend="fsevents",
        poll_seconds=0.1,
        quiet_seconds=0.3,
        full_scan_seconds=60.0,
        max_workers=4,
        folders=["/tmp/a", "/tmp/b"],
    )
    status.update_folder("/tmp/a", "dirty")
    status.update_folder("/tmp/b", "running")
    snapshot = read_watch_status()
    assert snapshot["backend"] == "fsevents"
    assert snapshot["pending_count"] == 1
    assert snapshot["running_count"] == 1
    status.update_folder("/tmp/a", "idle")
    status.stop("test complete")
    assert read_watch_status()["active"] is False


def test_quick_control_opens_command_center(monkeypatch, tmp_path: Path) -> None:
    launched: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(quick_controls.subprocess, "Popen", fake_popen)
    ok, message = quick_controls.open_app(str(tmp_path))

    assert ok is True
    assert message == "File Organizer opened"
    command = launched["command"]
    assert isinstance(command, list)
    assert command[1].endswith("command_center.py")
    assert command[-4:] == ["--path", str(tmp_path), "--page", "organize"]
