"""Regression tests for file safety, recovery, and incremental routing."""
import json
import os
from pathlib import Path

import pytest

import org_paths
from org_manifest import RecoveryJournal, is_manifest_active, read_manifest_data, restore_from_manifest
from org_organizer import Organizer, OrganizerCancelled
from rename_files_randomly import RandomRenamer


def organizer(root, **options):
    defaults = dict(base=root, recursive=True, strategy="flatten-root", include_hidden=False,
                    normalize="none", collect_empty_dirs=False, dry_run=False, create_backup=True)
    defaults.update(options)
    return Organizer(**defaults)


def test_case_variant_preserves_existing_content(tmp_path):
    (tmp_path / "Images").mkdir()
    (tmp_path / "Images/PHOTO.JPG").write_text("keeper")
    (tmp_path / "photo.jpg").write_text("arrival")
    result = organizer(tmp_path).run()
    assert (tmp_path / "Images/PHOTO.JPG").read_text() == "keeper"
    assert (tmp_path / "Images/photo_1.jpg").read_text() == "arrival"
    assert result["name_collisions_resolved"] == 1


def test_atomic_collision_retry_keeps_concurrent_file(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "target"
    source.write_text("arrival")
    original = org_paths._rename_exclusive
    first = True
    def race(src, dst):
        nonlocal first
        if first:
            first = False
            dst.write_text("concurrent keeper")
        return original(src, dst)
    monkeypatch.setattr(org_paths, "_rename_exclusive", race)
    actual = org_paths.move_to_available(source, destination)
    assert destination.read_text() == "concurrent keeper"
    assert actual.read_text() == "arrival"
    assert actual != destination


def test_undo_allocates_multiple_collision_suffixes(tmp_path):
    source = tmp_path / "photo.jpg"
    source.write_text("original")
    result = organizer(tmp_path).run()
    source.write_text("new")
    (tmp_path / "photo_restored.jpg").write_text("older restore")
    (tmp_path / "photo_restored_1.jpg").write_text("another restore")
    assert restore_from_manifest(result["backup_manifest"])
    assert source.read_text() == "new"
    assert (tmp_path / "photo_restored.jpg").read_text() == "older restore"
    assert (tmp_path / "photo_restored_1.jpg").read_text() == "another restore"
    assert (tmp_path / "photo_restored_2.jpg").read_text() == "original"


def test_failure_after_first_move_retains_recovery(tmp_path, monkeypatch):
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_text(name)
    run = organizer(tmp_path)
    original = run._fast_move
    count = 0
    def fail_second(src, dst):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("simulated disk error")
        return original(src, dst)
    monkeypatch.setattr(run, "_fast_move", fail_second)
    with pytest.raises(OSError):
        run.run()
    manifest = Path(run._recovery_info["manifest"])
    assert json.loads(manifest.read_text())["status"] == "failed"
    assert restore_from_manifest(str(manifest))
    assert (tmp_path / "a.jpg").read_text() == "a.jpg"
    assert (tmp_path / "b.jpg").read_text() == "b.jpg"


def test_pending_rename_recovered_after_missing_completion(tmp_path, monkeypatch):
    source = tmp_path / "before.jpg"
    source.write_text("original")
    journal = RecoveryJournal(tmp_path)
    append = journal._append
    def lost_completion(event):
        if event["event"] == "done":
            raise KeyboardInterrupt()
        append(event)
    monkeypatch.setattr(journal, "_append", lost_completion)
    with pytest.raises(KeyboardInterrupt):
        journal.move(source, tmp_path / "after.jpg")
    journal._lock.close()  # simulate process exit releasing the kernel lock
    assert read_manifest_data(journal.manifest_path)["file_moves"]
    assert restore_from_manifest(str(journal.manifest_path))
    assert source.read_text() == "original"


def test_active_run_cannot_be_restored(tmp_path):
    source = tmp_path / "before.jpg"
    source.write_text("original")
    journal = RecoveryJournal(tmp_path)
    journal.move(source, tmp_path / "after.jpg")
    if os.name != "nt":
        assert is_manifest_active(journal.manifest_path)
        assert not restore_from_manifest(str(journal.manifest_path))
    journal.finish()
    assert not is_manifest_active(journal.manifest_path)
    assert restore_from_manifest(str(journal.manifest_path))


def test_modified_destination_is_not_moved_by_undo(tmp_path):
    (tmp_path / "photo.jpg").write_text("original")
    result = organizer(tmp_path).run()
    (tmp_path / "Images/photo.jpg").write_text("edited later")
    assert not restore_from_manifest(result["backup_manifest"])
    assert (tmp_path / "Images/photo.jpg").read_text() == "edited later"


def test_cancel_between_moves_has_recovery(tmp_path, monkeypatch):
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_text(name)
    run = organizer(tmp_path)
    original = run._fast_move
    def cancel_after(src, dst):
        target = original(src, dst)
        run.cancel_requested = True
        return target
    monkeypatch.setattr(run, "_fast_move", cancel_after)
    with pytest.raises(OrganizerCancelled):
        run.run()
    assert restore_from_manifest(run._recovery_info["manifest"])
    assert (tmp_path / "a.jpg").exists() and (tmp_path / "b.jpg").exists()


def test_rename_protects_entire_excluded_subtrees(tmp_path):
    protected = ["For Deletion/saved/keep.jpg", "Needs Review/creator/keep.jpg",
                 "Duplicates/deep/keep.jpg", ".hidden/sub/keep.jpg", "excluded/sub/keep.jpg"]
    for name in protected:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("protected")
    (tmp_path / "incoming.jpg").write_text("incoming")
    result = organizer(tmp_path, random_names_after_organize=True, exclude_patterns=["excluded"]).run()
    for name in protected:
        assert (tmp_path / name).read_text() == "protected"
    assert result["rename_after_organize"]["files_renamed"] == 1
    assert restore_from_manifest(result["backup_manifest"])
    assert (tmp_path / "incoming.jpg").read_text() == "incoming"


@pytest.mark.parametrize("strategy", ["flatten-root", "in-place"])
@pytest.mark.parametrize("rename", [False, True])
def test_delta_routing_and_naming_avoid_full_walk(tmp_path, monkeypatch, strategy, rename):
    album = tmp_path / "album"
    album.mkdir()
    source = album / "incoming.jpg"
    source.write_text("incoming")
    (tmp_path / "unrelated.jpg").write_text("untouched")
    run = organizer(tmp_path, strategy=strategy, random_names_after_organize=rename, changed_paths=[source])
    monkeypatch.setattr(os, "walk", lambda *a, **k: pytest.fail("delta walked the full tree"))
    result = run.run()
    assert result["execution"] == "incremental"
    assert (tmp_path / "unrelated.jpg").read_text() == "untouched"
    destination = (album if strategy == "in-place" else tmp_path) / "Images"
    assert [p.read_text() for p in destination.iterdir()] == ["incoming"]
    assert restore_from_manifest(result["backup_manifest"])
    assert source.read_text() == "incoming"


def test_delta_empty_cleanup_touches_only_ancestors(tmp_path, monkeypatch):
    leaf = tmp_path / "changed/leaf"
    leaf.mkdir(parents=True)
    (tmp_path / "unrelated/empty").mkdir(parents=True)
    run = organizer(tmp_path, empty_dirs_only=True, changed_paths=[leaf])
    monkeypatch.setattr(os, "walk", lambda *a, **k: pytest.fail("delta walked the full tree"))
    result = run.run()
    assert result["execution"] == "incremental"
    assert not (tmp_path / "changed").exists()
    assert (tmp_path / "unrelated/empty").is_dir()
    assert restore_from_manifest(result["backup_manifest"])
    assert leaf.is_dir()


def test_readiness_skips_explicit_recent_and_partial_files(tmp_path):
    for name in ("recent.jpg", "explicit.jpg", "copy.crdownload"):
        (tmp_path / name).write_text(name)
    (tmp_path / "package.download").mkdir()
    (tmp_path / "package.download/nested.jpg").write_text("partial")
    result = organizer(tmp_path, deferred_paths=[tmp_path / "explicit.jpg"], file_stable_seconds=60).run()
    assert result["files_moved"] == 0
    assert result["deferred_files"] == 4
    assert (tmp_path / "package.download/nested.jpg").exists()


def test_random_standalone_preserves_protected_folders_and_recovers(tmp_path):
    protected = tmp_path / "For Deletion/nested/keep.jpg"
    protected.parent.mkdir(parents=True)
    protected.write_text("keep")
    (tmp_path / "source.jpg").write_text("source")
    result = RandomRenamer(tmp_path).run()
    assert protected.read_text() == "keep"
    assert restore_from_manifest(result["backup_manifest"])
    assert (tmp_path / "source.jpg").read_text() == "source"


def test_partial_package_does_not_schedule_continuous_recheck(tmp_path):
    (tmp_path / "video.download").mkdir()
    (tmp_path / "video.download/part.mp4").write_text("downloading")
    result = organizer(tmp_path, file_stable_seconds=1).run()
    assert result["deferred_files"] == 1
    assert result["file_recheck_seconds"] is None


def test_cross_volume_copy_is_published_exclusively(tmp_path, monkeypatch):
    import errno
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"content")
    original = org_paths._rename_exclusive
    def cross_volume(src, dst):
        if src == source:
            raise OSError(errno.EXDEV, "different volume")
        original(src, dst)
    monkeypatch.setattr(org_paths, "_rename_exclusive", cross_volume)
    org_paths.move_no_replace(source, destination)
    assert destination.read_bytes() == b"content"
    assert not source.exists()


def test_cross_volume_changing_source_is_not_unlinked(tmp_path, monkeypatch):
    import errno
    import shutil
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"initial")
    def cross_volume(src, dst):
        raise OSError(errno.EXDEV, "different volume")
    copy = shutil.copyfileobj
    def grow(incoming, output, length):
        copy(incoming, output, length)
        source.write_bytes(b"new content during copy")
    monkeypatch.setattr(org_paths, "_rename_exclusive", cross_volume)
    monkeypatch.setattr(shutil, "copyfileobj", grow)
    with pytest.raises(OSError):
        org_paths.move_no_replace(source, destination)
    assert source.read_bytes() == b"new content during copy"
    assert not destination.exists()


@pytest.mark.parametrize("removed", [["../outside"], ["/outside"]])
def test_restore_rejects_removed_directories_outside_root(tmp_path, removed):
    marker = tmp_path / "manifest.json"
    marker.write_text(json.dumps({"base_path": str(tmp_path), "empty_dirs_removed": removed}))
    assert not restore_from_manifest(str(marker))


def test_restore_rejects_symlink_escape(tmp_path):
    base, outside = tmp_path / "base", tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / "escape").symlink_to(outside, target_is_directory=True)
    marker = base / "manifest.json"
    marker.write_text(json.dumps({"base_path": str(base), "empty_dirs_removed": ["escape/new"]}))
    assert not restore_from_manifest(str(marker))
    assert not (outside / "new").exists()


def test_undo_follows_renaming_collision_chain(tmp_path):
    (tmp_path / "source.jpg").write_text("original")
    result = organizer(tmp_path, random_names_after_organize=True).run()
    (tmp_path / "Images/source.jpg").write_text("later arrival")
    assert restore_from_manifest(result["backup_manifest"])
    assert (tmp_path / "source.jpg").read_text() == "original"
    assert (tmp_path / "Images/source.jpg").read_text() == "later arrival"


def test_in_place_keeps_root_buckets_idempotent(tmp_path):
    (tmp_path / "Images").mkdir()
    photo = tmp_path / "Images/photo.jpg"
    photo.write_text("keep")
    assert organizer(tmp_path, strategy="in-place").run()["files_moved"] == 0
    assert organizer(tmp_path, strategy="in-place", changed_paths=[photo]).run()["files_moved"] == 0
    assert photo.read_text() == "keep"
    assert not (tmp_path / "Images/Images").exists()


def test_cross_volume_published_copy_is_visible_after_crash(tmp_path, monkeypatch):
    import errno
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_text("original")
    native = org_paths._rename_exclusive
    def cross_volume(src, dst):
        if src == source:
            raise OSError(errno.EXDEV, "different volume")
        native(src, dst)
    monkeypatch.setattr(org_paths, "_rename_exclusive", cross_volume)
    journal = RecoveryJournal(tmp_path)
    append = journal._append
    def crash_before_done(event):
        if event["event"] == "done":
            raise KeyboardInterrupt()
        append(event)
    monkeypatch.setattr(journal, "_append", crash_before_done)
    with pytest.raises(KeyboardInterrupt):
        journal.move(source, destination)
    journal._lock.close()
    assert source.read_text() == destination.read_text() == "original"
    assert read_manifest_data(journal.manifest_path)["file_moves"][0]["to"] == "destination"


def test_watch_receipt_does_not_absorb_later_user_changes(tmp_path):
    source = tmp_path / "photo.jpg"
    source.write_text("original")
    result = organizer(tmp_path).run()
    target = tmp_path / "Images/photo.jpg"
    receipt = result["watch_mutations"][str(target)]
    assert result["watch_mutations"][str(source)] is None
    assert receipt["inode"] == target.stat().st_ino
    target.write_text("a later user edit")
    assert receipt["size"] != target.stat().st_size
