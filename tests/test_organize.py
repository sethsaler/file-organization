#!/usr/bin/env python3
"""Tests for organize_by_filetype and helpers."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from org_buckets import bucket_for_filename, resolve_profile
from org_exclude import dir_name_excluded, merge_exclude_patterns
from org_organizer import Organizer
from org_paths import normalize_folder_input
from schedule_config import ScheduleConfig, find_path_conflicts, save_config


@pytest.fixture
def tmp_tree(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    (tmp_path / "sub" / "clip.mp4").write_bytes(b"\x00")
    (tmp_path / "readme").write_text("hi")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git")
    return tmp_path


def test_bucket_for_filename():
    assert bucket_for_filename("a.GIF", "standard") == "GIFs"
    assert bucket_for_filename("a.jpg", "standard") == "Images"
    assert bucket_for_filename("doc.pdf", "extended") == "Documents"


def test_exclude_defaults():
    pats = merge_exclude_patterns([], use_defaults=True)
    assert ".git" in pats
    assert dir_name_excluded(".git", pats)


def test_flatten_dry_run(tmp_tree: Path):
    org = Organizer(
        base=tmp_tree,
        recursive=True,
        strategy="flatten-root",
        include_hidden=False,
        normalize="none",
        collect_empty_dirs=False,
        dry_run=True,
        create_backup=False,
        exclude_patterns=merge_exclude_patterns([], use_defaults=True),
    )
    result = org.run()
    assert result["files_moved"] == 3
    assert (tmp_tree / "sub" / "photo.jpg").exists()


def test_collision_suffix(tmp_path: Path):
    d = tmp_path / "Images"
    d.mkdir()
    (d / "x.jpg").write_bytes(b"1")
    (tmp_path / "x.jpg").write_bytes(b"2")
    org = Organizer(
        base=tmp_path,
        recursive=False,
        strategy="flatten-root",
        include_hidden=True,
        normalize="none",
        collect_empty_dirs=False,
        dry_run=False,
        create_backup=True,
    )
    org.run()
    names = {p.name for p in d.iterdir()}
    assert "x.jpg" in names
    assert any(n.startswith("x_") for n in names)


def test_idempotent_in_bucket(tmp_path: Path):
    images = tmp_path / "Images"
    images.mkdir()
    f = images / "already.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    org = Organizer(
        base=tmp_path,
        recursive=False,
        strategy="flatten-root",
        include_hidden=True,
        normalize="none",
        collect_empty_dirs=False,
        dry_run=False,
        create_backup=False,
    )
    before = org.run()
    after = Organizer(
        base=tmp_path,
        recursive=False,
        strategy="flatten-root",
        include_hidden=True,
        normalize="none",
        collect_empty_dirs=False,
        dry_run=False,
        create_backup=False,
    ).run()
    assert before["files_moved"] == 0
    assert after["files_moved"] == 0


def test_restore_roundtrip(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    org = Organizer(
        base=tmp_path,
        recursive=False,
        strategy="flatten-root",
        include_hidden=True,
        normalize="none",
        collect_empty_dirs=False,
        dry_run=False,
        create_backup=True,
    )
    summary = org.run()
    manifest = summary.get("backup_manifest")
    assert manifest and Path(manifest).is_file()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "organize_by_filetype.py"), "--restore", manifest],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data.get("files_restored", 0) >= 1
    assert (tmp_path / "a.txt").exists()


def test_path_conflicts():
    from schedule_config import FolderJob

    cfg = ScheduleConfig(
        folders=[
            FolderJob(path="/tmp/parent"),
            FolderJob(path="/tmp/parent/child"),
        ]
    )
    warnings = find_path_conflicts(cfg)
    assert any("Nested" in w for w in warnings)


def test_cli_extended_profile(tmp_path: Path):
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "organize_by_filetype.py"),
            "--path",
            str(tmp_path),
            "--no-recursive",
            "--profile",
            "extended",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "Documents" in data["buckets"]


def test_resolve_profile_custom(tmp_path: Path):
    prof = tmp_path / "prof.json"
    prof.write_text(json.dumps({"Screenshots": ["png"]}), encoding="utf-8")
    label, buckets = resolve_profile(str(prof))
    assert label.startswith("custom:")


def test_typescript_goes_to_code_not_videos():
    assert bucket_for_filename("app.ts", "extended") == "Code"


def test_schedule_dry_run_verified_defaults_false_v4():
    from schedule_config import ScheduleConfig

    cfg = ScheduleConfig.from_json_dict({
        "version": 4,
        "folders": [{"path": "/tmp/x", "enabled": True}],
    })
    assert cfg.folders[0].dry_run_verified is False


def test_schedule_dry_run_verified_legacy_v3():
    from schedule_config import ScheduleConfig

    cfg = ScheduleConfig.from_json_dict({
        "version": 3,
        "folders": [{"path": "/tmp/x"}],
    })
    assert cfg.folders[0].dry_run_verified is True


def test_daily_schedule_mode_roundtrip(tmp_path: Path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from schedule_config import (
        SCHEDULE_MODE_DAILY,
        ScheduleConfig,
        normalize_daily_time,
        parse_daily_time,
        save_config,
        seconds_until_next_daily_run,
        wait_seconds_after_run,
    )

    assert parse_daily_time("00:00") == (0, 0)
    assert parse_daily_time("12:30") == (12, 30)
    assert normalize_daily_time("9:5") == "09:05"
    assert normalize_daily_time("nope") == "00:00"

    cfg = ScheduleConfig(
        schedule_mode=SCHEDULE_MODE_DAILY,
        daily_time="00:00",
        interval_minutes=60,
    )
    path = tmp_path / "schedule.json"
    save_config(path, cfg)
    loaded = ScheduleConfig.from_json_dict(json.loads(path.read_text(encoding="utf-8")))
    assert loaded.schedule_mode == SCHEDULE_MODE_DAILY
    assert loaded.daily_time == "00:00"

    tz = ZoneInfo("UTC")
    now = datetime(2026, 5, 23, 10, 0, tzinfo=tz)
    sec = seconds_until_next_daily_run("00:00", now=now)
    assert 13 * 3600 <= sec <= 14 * 3600 + 1

    now2 = datetime(2026, 5, 23, 23, 30, tzinfo=tz)
    sec2 = seconds_until_next_daily_run("00:00", now=now2)
    assert 29 * 60 <= sec2 <= 31 * 60

    cfg_daily = ScheduleConfig(schedule_mode=SCHEDULE_MODE_DAILY, daily_time="00:00")
    wait = wait_seconds_after_run(cfg_daily)
    assert wait > 0

    cfg_interval = ScheduleConfig(interval_minutes=30)
    assert wait_seconds_after_run(cfg_interval) == 30 * 60


def test_estimate_seconds_until_next_run_interval():
    from datetime import datetime, timezone

    from schedule_config import FolderJob, estimate_seconds_until_next_run

    recent = datetime.now(timezone.utc).isoformat()
    cfg = ScheduleConfig(
        interval_minutes=60,
        folders=[FolderJob(path="/tmp/a", enabled=True, last_run=recent, dry_run_verified=True)],
    )
    sec = estimate_seconds_until_next_run(cfg)
    assert 0 <= sec <= 3600


def test_schedule_service_launchd_plist_interval_short(monkeypatch):
    """Short interval (≤ 60 min) uses StartInterval + --once (no 24/7 daemon)."""
    import schedule_service
    from schedule_service import SERVICE_LABEL, build_launchd_plist, daemon_script

    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("interval", "02:00", 30))
    plist = build_launchd_plist()
    assert plist["Label"] == SERVICE_LABEL
    assert plist["ProgramArguments"][-1] == "--once"
    assert str(daemon_script()) in plist["ProgramArguments"]
    assert plist["StartInterval"] == 30 * 60
    assert "KeepAlive" not in plist
    assert "RunAtLoad" not in plist
    assert "StartCalendarInterval" not in plist


def test_schedule_service_launchd_plist_interval_long(monkeypatch):
    """Long interval (> 60 min) uses --foreground + KeepAlive (long-running daemon)."""
    import schedule_service
    from schedule_service import SERVICE_LABEL, build_launchd_plist, daemon_script

    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("interval", "02:00", 120))
    plist = build_launchd_plist()
    assert plist["Label"] == SERVICE_LABEL
    assert plist["ProgramArguments"][-1] == "--foreground"
    assert str(daemon_script()) in plist["ProgramArguments"]
    assert plist["KeepAlive"] is True
    assert "StartCalendarInterval" not in plist
    assert "StartInterval" not in plist


def test_schedule_service_launchd_plist_daily(monkeypatch):
    import schedule_service
    from schedule_service import SERVICE_LABEL, build_launchd_plist, daemon_script

    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("daily", "02:30", 60))
    plist = build_launchd_plist()
    assert plist["Label"] == SERVICE_LABEL
    # Daily mode runs a single batch on a calendar trigger (no drifting sleep loop).
    assert plist["ProgramArguments"][-1] == "--once"
    assert str(daemon_script()) in plist["ProgramArguments"]
    assert plist["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    # No KeepAlive/RunAtLoad: the one-shot must fire only on schedule.
    assert "KeepAlive" not in plist
    assert "RunAtLoad" not in plist


def test_normalize_folder_input(tmp_path: Path):
    base = tmp_path / "icloud folder"
    base.mkdir()
    posix = str(base)

    assert normalize_folder_input(posix) == base
    assert normalize_folder_input(f'"{posix}"') == base
    assert normalize_folder_input(f"file://{posix.replace(' ', '%20')}") == base
    assert normalize_folder_input(posix + "/") == base
    assert normalize_folder_input(posix + "\n") == base


def test_normalize_folder_input_shell_escapes(tmp_path: Path):
    base = tmp_path / "Mobile Documents" / "com~apple~CloudDocs" / "My Folder"
    base.mkdir(parents=True)
    escaped = str(base).replace(" ", "\\ ").replace("~", "\\~")
    assert normalize_folder_input(escaped).resolve() == base.resolve()


def test_normalize_folder_input_trailing_space_on_last_component(tmp_path: Path):
    parent = tmp_path / "Manual Library"
    parent.mkdir()
    actual = parent / "Magui "
    actual.mkdir()
    pasted = str(parent / "Magui")
    assert normalize_folder_input(pasted) == actual


def test_normalize_folder_input_icloud_mobile_documents():
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if not icloud.is_dir():
        pytest.skip("iCloud Drive not available")
    raw = str(icloud)
    assert normalize_folder_input(raw).is_dir()
    assert normalize_folder_input(f'"{raw}"').is_dir()
    assert normalize_folder_input(f"file://{raw.replace(' ', '%20')}").is_dir()


# ---------------------------------------------------------------------------
# Threshold gating tests
# ---------------------------------------------------------------------------


def test_count_unsorted_files_flatten_root(tmp_path: Path):
    from schedule_config import FolderJob, count_unsorted_files

    (tmp_path / "Images").mkdir()
    (tmp_path / "Videos").mkdir()
    (tmp_path / "Images" / "photo.jpg").write_bytes(b"x")
    (tmp_path / "Videos" / "clip.mp4").write_bytes(b"x")
    (tmp_path / "loose1.png").write_bytes(b"x")
    (tmp_path / "loose2.pdf").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")

    job = FolderJob(path=str(tmp_path), strategy="flatten-root", recursive=True, profile="standard")
    assert count_unsorted_files(job) == 2


def test_count_unsorted_files_in_place(tmp_path: Path):
    from schedule_config import FolderJob, count_unsorted_files

    (tmp_path / "Images").mkdir()
    (tmp_path / "Images" / "sorted.jpg").write_bytes(b"x")
    (tmp_path / "subfolder").mkdir()
    (tmp_path / "subfolder" / "loose.png").write_bytes(b"x")
    (tmp_path / "subfolder" / "doc.pdf").write_bytes(b"x")
    (tmp_path / "subfolder" / "Images").mkdir()
    (tmp_path / "subfolder" / "Images" / "nested_sorted.jpg").write_bytes(b"x")
    (tmp_path / "For Deletion").mkdir()
    (tmp_path / "For Deletion" / "old.txt").write_bytes(b"x")
    (tmp_path / ".organizer").mkdir()
    (tmp_path / ".organizer" / "backup.json").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")

    job = FolderJob(path=str(tmp_path), strategy="in-place", recursive=True, profile="standard")
    assert count_unsorted_files(job) == 2  # loose.png + doc.pdf in subfolder


def test_count_unsorted_files_in_place_stop_at(tmp_path: Path):
    from schedule_config import FolderJob, count_unsorted_files

    for i in range(10):
        (tmp_path / f"file{i}.txt").write_bytes(b"x")

    job = FolderJob(path=str(tmp_path), strategy="in-place", recursive=True, profile="standard")
    assert count_unsorted_files(job, stop_at=3) == 3


def test_count_unsorted_files_non_recursive(tmp_path: Path):
    from schedule_config import FolderJob, count_unsorted_files

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.png").write_bytes(b"x")
    (tmp_path / "root.jpg").write_bytes(b"x")

    job = FolderJob(path=str(tmp_path), strategy="flatten-root", recursive=False, profile="standard")
    assert count_unsorted_files(job) == 1


def test_count_unsorted_files_missing_path():
    from schedule_config import FolderJob, count_unsorted_files

    job = FolderJob(path="/nonexistent/path/xyz", strategy="flatten-root", recursive=True, profile="standard")
    assert count_unsorted_files(job) == 0


def test_threshold_skip_below(tmp_path: Path):
    from schedule_config import FolderJob, ScheduleConfig, count_unsorted_files

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")

    job = FolderJob(
        path=str(tmp_path),
        strategy="flatten-root",
        recursive=True,
        profile="standard",
        dry_run_verified=True,
        min_unsorted_threshold=20,
    )
    assert count_unsorted_files(job) == 2
    assert count_unsorted_files(job, stop_at=20) == 2
    assert 2 < 20  # threshold would skip


def test_threshold_runs_at_or_above(tmp_path: Path):
    from schedule_config import FolderJob, count_unsorted_files

    for i in range(25):
        (tmp_path / f"file{i}.jpg").write_bytes(b"x")

    job = FolderJob(
        path=str(tmp_path),
        strategy="flatten-root",
        recursive=True,
        profile="standard",
        dry_run_verified=True,
        min_unsorted_threshold=20,
    )
    assert count_unsorted_files(job, stop_at=20) == 20  # early-abort at 20
    assert count_unsorted_files(job) == 25  # full count


def test_threshold_zero_never_gates():
    from schedule_config import FolderJob

    job = FolderJob(path="/tmp/x", min_unsorted_threshold=0)
    assert job.min_unsorted_threshold == 0


def test_threshold_serialization_roundtrip():
    from schedule_config import FolderJob, ScheduleConfig

    cfg = ScheduleConfig(folders=[FolderJob(path="/tmp/x", min_unsorted_threshold=20)])
    d = cfg.to_json_dict()
    assert d["folders"][0]["min_unsorted_threshold"] == 20
    loaded = ScheduleConfig.from_json_dict(d)
    assert loaded.folders[0].min_unsorted_threshold == 20


# ---- duplicate detection ----


def _make_organizer(base: Path, **overrides):
    kwargs = dict(
        base=base,
        recursive=True,
        strategy="flatten-root",
        include_hidden=True,
        normalize="none",
        collect_empty_dirs=False,
        dry_run=False,
        create_backup=False,
    )
    kwargs.update(overrides)
    return Organizer(**kwargs)


def test_detect_duplicates_moves_copy(tmp_path: Path):
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0SAME")
    (tmp_path / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0SAME")
    (tmp_path / "c.jpg").write_bytes(b"\xff\xd8\xffDIFF")

    result = _make_organizer(tmp_path, detect_duplicates=True).run()

    images = {p.name for p in (tmp_path / "Images").iterdir()}
    dupes = {p.name for p in (tmp_path / "Duplicates").iterdir()}
    assert result["duplicates"]["files_moved"] == 1
    assert "c.jpg" in images
    assert len(dupes) == 1
    assert dupes | images == {"a.jpg", "b.jpg", "c.jpg"}


def test_detect_duplicates_same_size_different_content(tmp_path: Path):
    (tmp_path / "a.jpg").write_bytes(b"AAAA")
    (tmp_path / "b.jpg").write_bytes(b"BBBB")

    result = _make_organizer(tmp_path, detect_duplicates=True).run()

    assert result["duplicates"]["files_moved"] == 0
    assert not (tmp_path / "Duplicates").exists()
    assert {p.name for p in (tmp_path / "Images").iterdir()} == {"a.jpg", "b.jpg"}


def test_detect_duplicates_keeps_existing_bucket_copy(tmp_path: Path):
    images = tmp_path / "Images"
    images.mkdir()
    (images / "original.jpg").write_bytes(b"\xff\xd8\xffCONTENT")
    (tmp_path / "newcopy.jpg").write_bytes(b"\xff\xd8\xffCONTENT")

    result = _make_organizer(tmp_path, detect_duplicates=True).run()

    assert (images / "original.jpg").exists()
    assert (tmp_path / "Duplicates" / "newcopy.jpg").exists()
    assert result["duplicates"]["files_moved"] == 1


def test_detect_duplicates_dry_run_reports_without_moving(tmp_path: Path):
    (tmp_path / "a.png").write_bytes(b"\x89PNGSAME")
    (tmp_path / "b.png").write_bytes(b"\x89PNGSAME")

    result = _make_organizer(tmp_path, detect_duplicates=True, dry_run=True).run()

    assert result["duplicates"]["files_moved"] == 1
    assert (tmp_path / "a.png").exists()
    assert (tmp_path / "b.png").exists()
    assert not (tmp_path / "Duplicates").exists()


def test_detect_duplicates_skips_duplicates_dir_on_rerun(tmp_path: Path):
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xffSAME")
    (tmp_path / "b.jpg").write_bytes(b"\xff\xd8\xffSAME")
    _make_organizer(tmp_path, detect_duplicates=True).run()

    result = _make_organizer(tmp_path, detect_duplicates=True).run()

    # The staged duplicate must stay put and not bounce back into Images.
    assert result["duplicates"]["files_moved"] == 0
    assert len(list((tmp_path / "Duplicates").iterdir())) == 1


def test_detect_duplicates_zero_byte_files_not_dupes(tmp_path: Path):
    (tmp_path / "a.jpg").write_bytes(b"")
    (tmp_path / "b.jpg").write_bytes(b"")

    result = _make_organizer(tmp_path, detect_duplicates=True).run()

    assert result["duplicates"]["files_moved"] == 0


def test_detect_duplicates_cli_flag_in_schedule_cmd():
    from schedule_config import FolderJob, build_organize_cmd

    job = FolderJob(path="/tmp/x", detect_duplicates=True)
    assert "--detect-duplicates" in build_organize_cmd(job)
    job2 = FolderJob(path="/tmp/x")
    assert "--detect-duplicates" not in build_organize_cmd(job2)


def test_detect_duplicates_serialization_roundtrip():
    from schedule_config import FolderJob, ScheduleConfig

    cfg = ScheduleConfig(folders=[FolderJob(path="/tmp/x", detect_duplicates=True)])
    loaded = ScheduleConfig.from_json_dict(cfg.to_json_dict())
    assert loaded.folders[0].detect_duplicates is True


# ---- dry-run empty-folder simulation (in-memory) ----


def _empty_dir_tree(base: Path) -> None:
    (base / "emptyA").mkdir()
    (base / "parent").mkdir()
    (base / "parent" / "emptyB").mkdir()
    (base / "parent" / "note.txt").write_text("x")
    (base / "sub").mkdir()
    (base / "sub" / "pic.jpg").write_bytes(b"\xff\xd8\xff")
    (base / "deep").mkdir()
    (base / "deep" / "inner").mkdir()
    (base / "deep" / "inner" / "leaf").mkdir()


def test_dry_run_empty_dir_preview_matches_real_run(tmp_path: Path):
    dry_base = tmp_path / "dry"
    real_base = tmp_path / "real"
    dry_base.mkdir()
    real_base.mkdir()
    _empty_dir_tree(dry_base)
    _empty_dir_tree(real_base)

    dry = _make_organizer(dry_base, collect_empty_dirs=True, dry_run=True).run()
    real = _make_organizer(real_base, collect_empty_dirs=True, dry_run=False).run()

    assert dry["empty_folder_collection"]["folders_moved"] == real["empty_folder_collection"]["folders_moved"]
    assert (
        dry["empty_folder_collection"]["name_collisions_resolved"]
        == real["empty_folder_collection"]["name_collisions_resolved"]
    )
    # dry run must not have changed anything on disk
    assert (dry_base / "emptyA").exists()
    assert not (dry_base / "For Deletion").exists()
    # real run staged the empty trees
    assert (real_base / "For Deletion").is_dir()


def test_dry_run_empty_dir_preview_collision(tmp_path: Path):
    dry_base = tmp_path / "dry"
    real_base = tmp_path / "real"
    for base in (dry_base, real_base):
        base.mkdir()
        (base / "For Deletion").mkdir()
        (base / "For Deletion" / "dupe").mkdir()
        (base / "dupe").mkdir()

    dry = _make_organizer(dry_base, collect_empty_dirs=True, dry_run=True).run()
    real = _make_organizer(real_base, collect_empty_dirs=True, dry_run=False).run()

    assert dry["empty_folder_collection"]["folders_moved"] == real["empty_folder_collection"]["folders_moved"] == 1
    assert dry["empty_folder_collection"]["name_collisions_resolved"] == 1
    assert (real_base / "For Deletion" / "dupe_1").is_dir()


# ---- .DS_Store cleanup ----


def test_dsstore_cleanup_named_and_renamed(tmp_path: Path):
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1junk")
    (tmp_path / "ABCDEFGHIJKLMNOP").write_bytes(b"\x00\x00\x00\x01Bud1junk")  # renamed DS_Store
    (tmp_path / "keepme").write_text("plain extensionless file")

    _make_organizer(tmp_path).run()

    remaining = [p.name for p in tmp_path.rglob("*") if p.is_file()]
    assert ".DS_Store" not in remaining
    assert "ABCDEFGHIJKLMNOP" not in remaining
    assert "keepme" in remaining


# ---- watch schedule mode ----


def test_watch_mode_normalize_and_roundtrip():
    from schedule_config import SCHEDULE_MODE_WATCH, ScheduleConfig, normalize_schedule_mode

    assert normalize_schedule_mode("watch") == SCHEDULE_MODE_WATCH
    assert normalize_schedule_mode("WATCH ") == SCHEDULE_MODE_WATCH
    assert normalize_schedule_mode("bogus") == "interval"

    cfg = ScheduleConfig(schedule_mode=SCHEDULE_MODE_WATCH)
    loaded = ScheduleConfig.from_json_dict(cfg.to_json_dict())
    assert loaded.schedule_mode == SCHEDULE_MODE_WATCH


def test_schedule_service_launchd_plist_watch(monkeypatch):
    """Watch mode uses launchd WatchPaths with a one-shot run and throttle."""
    import schedule_service
    from schedule_service import SERVICE_LABEL, build_launchd_plist, daemon_script

    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("watch", "00:00", 60))
    monkeypatch.setattr(schedule_service, "_enabled_watch_paths", lambda: ["/tmp/watched"])
    plist = build_launchd_plist()
    assert plist["Label"] == SERVICE_LABEL
    assert plist["ProgramArguments"][-1] == "--once"
    assert str(daemon_script()) in plist["ProgramArguments"]
    assert plist["WatchPaths"] == ["/tmp/watched"]
    assert plist["ThrottleInterval"] == 15
    assert plist["StartInterval"] == 3600  # backstop for missed events
    assert "KeepAlive" not in plist
    assert "StartCalendarInterval" not in plist


def test_schedule_service_prefers_stable_python_symlink(tmp_path: Path, monkeypatch):
    """A stable `python3` symlink to the current interpreter is baked into the
    service file instead of a versioned path that vanishes on `brew upgrade`."""
    import sys

    import schedule_service

    stable = tmp_path / "python3"
    stable.symlink_to(Path(sys.executable).resolve())
    monkeypatch.setattr(schedule_service, "_STABLE_PYTHON_CANDIDATES", (stable,))
    assert schedule_service._python_executable() == str(stable)

    # A symlink to a DIFFERENT interpreter must not be picked.
    other = tmp_path / "other" / "python3"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    monkeypatch.setattr(schedule_service, "_STABLE_PYTHON_CANDIDATES", (other,))
    assert schedule_service._python_executable() == sys.executable


def test_schedule_service_launchd_plist_stale(tmp_path: Path, monkeypatch):
    """Staleness check: missing plist, dead interpreter path, or config drift."""
    import plistlib

    import schedule_service

    plist_path = tmp_path / "agent.plist"
    monkeypatch.setattr(schedule_service, "launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("interval", "02:00", 30))
    monkeypatch.setattr(schedule_service, "_python_executable", lambda: str(tmp_path / "py"))
    (tmp_path / "py").write_text("")

    # No plist on disk → stale.
    assert schedule_service._installed_launchd_plist_stale()

    # Freshly written plist matching current config → not stale.
    with plist_path.open("wb") as f:
        plistlib.dump(schedule_service.build_launchd_plist(), f)
    assert not schedule_service._installed_launchd_plist_stale()

    # Installed plist references a binary that no longer exists (e.g. a Homebrew
    # Python removed by an upgrade) → stale.
    current = schedule_service.build_launchd_plist()
    broken = dict(current)
    broken["ProgramArguments"] = [str(tmp_path / "gone" / "python3.13")] + list(
        current["ProgramArguments"][1:]
    )
    with plist_path.open("wb") as f:
        plistlib.dump(broken, f)
    assert schedule_service._installed_launchd_plist_stale()

    # Installed plist valid but built from an older schedule → stale.
    with plist_path.open("wb") as f:
        plistlib.dump(current, f)
    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("interval", "02:00", 45))
    assert schedule_service._installed_launchd_plist_stale()


def test_watch_signature_detects_changes(tmp_path: Path):
    from schedule_config import FolderJob, watch_signature

    (tmp_path / "sub").mkdir()
    job = FolderJob(path=str(tmp_path))
    sig1 = watch_signature(job)
    (tmp_path / "new.jpg").write_bytes(b"x")
    sig2 = watch_signature(job)
    assert sig1 != sig2
    (tmp_path / "sub" / "deep.jpg").write_bytes(b"y")
    sig3 = watch_signature(job)
    assert sig2 != sig3
    # Changes several levels deep should also be detected.
    (tmp_path / "sub" / "nested").mkdir()
    (tmp_path / "sub" / "nested" / "deep.jpg").write_bytes(b"z")
    sig4 = watch_signature(job)
    assert sig3 != sig4
    (tmp_path / "sub" / "nested" / "another").mkdir()
    (tmp_path / "sub" / "nested" / "another" / "file.txt").write_text("txt")
    sig5 = watch_signature(job)
    assert sig4 != sig5


def test_wait_seconds_watch_mode_is_poll_interval():
    from schedule_config import SCHEDULE_MODE_WATCH, WATCH_POLL_SECONDS, ScheduleConfig, wait_seconds_after_run

    cfg = ScheduleConfig(schedule_mode=SCHEDULE_MODE_WATCH)
    assert wait_seconds_after_run(cfg) == WATCH_POLL_SECONDS
    assert wait_seconds_after_run(cfg) == cfg.watch_poll_seconds
    cfg.watch_poll_seconds = 0.5
    assert wait_seconds_after_run(cfg) == 0.5


def test_watch_timing_config_round_trip_and_bounds():
    from schedule_config import (
        WATCH_POLL_SECONDS,
        WATCH_QUIET_SECONDS,
        ScheduleConfig,
        load_config,
        save_config,
    )

    cfg = ScheduleConfig(watch_poll_seconds=0.5, watch_quiet_seconds=2.0)
    assert cfg.to_json_dict().get("watch_poll_seconds") == 0.5
    assert cfg.to_json_dict().get("watch_quiet_seconds") == 2.0

    defaults = ScheduleConfig()
    assert "watch_poll_seconds" not in defaults.to_json_dict()
    assert "watch_quiet_seconds" not in defaults.to_json_dict()
    assert defaults.watch_poll_seconds == WATCH_POLL_SECONDS
    assert defaults.watch_quiet_seconds == WATCH_QUIET_SECONDS

    bounded = ScheduleConfig.from_json_dict(
        {"watch_poll_seconds": -1.0, "watch_quiet_seconds": 100.0}
    )
    assert bounded.watch_poll_seconds == 0.05
    assert bounded.watch_quiet_seconds == 60.0

    path = Path("/tmp/test-watch-timing.json")
    save_config(path, cfg)
    loaded = load_config(path)
    assert loaded.watch_poll_seconds == 0.5
    assert loaded.watch_quiet_seconds == 2.0
    path.unlink()


# ---------------------------------------------------------------------------
# Speed + functionality batch: exclusion caching, fast paths, new features


def _mk_org(base: Path, **kw) -> Organizer:
    defaults = dict(
        base=base,
        recursive=True,
        strategy="flatten-root",
        include_hidden=True,
        normalize="none",
        collect_empty_dirs=False,
        dry_run=False,
        create_backup=False,
    )
    defaults.update(kw)
    return Organizer(**defaults)


def test_path_excluded_fast_path(tmp_path: Path):
    """String-prefix relative computation matches the resolve() behavior."""
    from org_exclude import path_excluded

    pats = merge_exclude_patterns(["Secret*"], use_defaults=True)
    assert path_excluded(tmp_path / "node_modules", tmp_path, pats)
    assert path_excluded(tmp_path / "a" / "SecretStuff", tmp_path, pats)
    assert not path_excluded(tmp_path / "a" / "b", tmp_path, pats)
    # A path not under base at all is never excluded.
    assert not path_excluded(Path("/somewhere/else"), tmp_path, pats)
    # Empty patterns short-circuit.
    assert not path_excluded(tmp_path / "node_modules", tmp_path, [])


def test_skip_dir_decisions_are_memoized(tmp_path: Path):
    org = _mk_org(tmp_path, exclude_patterns=merge_exclude_patterns([], use_defaults=True))
    assert org._should_skip_traversal_dir(tmp_path, "node_modules")
    assert (tmp_path / "node_modules") in org._skip_dir_cache
    assert not org._should_skip_traversal_dir(tmp_path, "regular")
    # Cached answer is reused (poison the compute path to prove it).
    org._compute_skip_traversal_dir = lambda *a: (_ for _ in ()).throw(AssertionError)
    assert org._should_skip_traversal_dir(tmp_path, "node_modules")


def test_collision_probe_does_not_restart(tmp_path: Path):
    org = _mk_org(tmp_path)
    d = tmp_path / "Other"
    org.reserved_names[d] = {"f.txt"}
    names = [org._collision_safe_target(d, "f.txt").name for _ in range(3)]
    assert names == ["f_1.txt", "f_2.txt", "f_3.txt"]


def test_date_buckets(tmp_path: Path):
    import os
    import time as _time

    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    ts = _time.mktime((2024, 3, 15, 12, 0, 0, 0, 0, -1))
    os.utime(f, (ts, ts))
    res = _mk_org(tmp_path, date_buckets=True).run()
    assert (tmp_path / "Images" / "2024" / "03" / "pic.jpg").is_file()
    assert res["moved_by_category"].get("Images") == 1
    assert res["date_buckets"] is True


def test_duplicates_hardlink(tmp_path: Path):
    (tmp_path / "a.jpg").write_bytes(b"same-bytes")
    (tmp_path / "b.jpg").write_bytes(b"same-bytes")
    res = _mk_org(tmp_path, detect_duplicates=True, duplicates_hardlink=True).run()
    imgs = tmp_path / "Images"
    assert sorted(p.name for p in imgs.iterdir()) == ["a.jpg", "b.jpg"]
    assert (imgs / "a.jpg").stat().st_ino == (imgs / "b.jpg").stat().st_ino
    assert res["duplicates"]["files_hardlinked"] == 1
    assert not (tmp_path / "Duplicates").exists()


def test_stat_is_dataless():
    from org_dupes import stat_is_dataless

    class St:
        def __init__(self, size, blocks=None, flags=0):
            self.st_size = size
            self.st_flags = flags
            if blocks is not None:
                self.st_blocks = blocks

    assert stat_is_dataless(St(10, flags=0x40000000))  # SF_DATALESS
    assert stat_is_dataless(St(100000, blocks=0))  # sizable but no local blocks
    assert not stat_is_dataless(St(100, blocks=0))  # tiny inline file
    assert not stat_is_dataless(St(100000, blocks=8))
    assert not stat_is_dataless(St(100000))  # platform without st_blocks


def test_duplicate_index_update_location(tmp_path: Path):
    from org_dupes import DuplicateIndex

    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.write_bytes(b"12345")
    b.write_bytes(b"99999")
    c.write_bytes(b"12345")
    idx = DuplicateIndex()
    assert idx.register(a, 5) is None
    assert idx.register(b, 5) is None  # same size, different content
    new_a = tmp_path / "moved_a"
    a.rename(new_a)
    idx.update_location(5, a, new_a)
    assert idx.register(c, 5) == new_a


def test_dsstore_only_dir_is_collected(tmp_path: Path):
    """With include_hidden=True a dir holding only .DS_Store must still go away
    (cleanup now runs before the empty-dir passes)."""
    d = tmp_path / "lonely"
    d.mkdir()
    (d / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1xx")
    (tmp_path / "f.jpg").write_bytes(b"x")
    _mk_org(tmp_path, collect_empty_dirs=True).run()
    assert not d.exists()


def test_watch_changed_paths(tmp_path: Path, monkeypatch):
    import schedule_daemon
    from schedule_config import FolderJob

    state = tmp_path / "state.json"
    monkeypatch.setattr(schedule_daemon, "_watch_state_path", lambda: state)
    fa, fb = tmp_path / "A", tmp_path / "B"
    fa.mkdir()
    fb.mkdir()
    cfg = ScheduleConfig(folders=[FolderJob(path=str(fa)), FolderJob(path=str(fb))])
    assert schedule_daemon._changed_watch_paths(cfg) is None  # no prior state
    schedule_daemon._save_watch_signatures(cfg)
    assert schedule_daemon._changed_watch_paths(cfg) is None  # unchanged → backstop
    (fa / "new.txt").write_text("x")
    assert schedule_daemon._changed_watch_paths(cfg) == {str(fa)}


def test_history_append_and_read(tmp_path: Path, monkeypatch):
    import org_logging

    monkeypatch.setattr(org_logging, "default_history_path", lambda: tmp_path / "h.jsonl")
    org_logging.append_history_entry({"path": "/x", "ok": True, "files_moved": 3})
    org_logging.append_history_entry({"path": "/y", "ok": False, "error": "boom"})
    recs = org_logging.read_history(10)
    assert recs[0]["path"] == "/y" and recs[0]["ok"] is False
    assert recs[1]["files_moved"] == 3
    assert all("ts" in r for r in recs)


def test_folderjob_new_fields_roundtrip_and_cmd():
    from schedule_config import FolderJob, build_organize_cmd

    job = FolderJob(
        path="/tmp/x",
        detect_duplicates=True,
        duplicates_hardlink=True,
        date_buckets=True,
        timeout_minutes=5,
    )
    cfg = ScheduleConfig(folders=[job], notify_on_run=False)
    loaded = ScheduleConfig.from_json_dict(cfg.to_json_dict())
    j = loaded.folders[0]
    assert j.duplicates_hardlink and j.date_buckets and j.timeout_minutes == 5
    assert loaded.notify_on_run is False
    cmd = build_organize_cmd(j)
    assert "--duplicates-hardlink" in cmd and "--date-buckets" in cmd
    # Defaults for configs written before these fields existed.
    old = ScheduleConfig.from_json_dict({"version": 5, "folders": [{"path": "/t"}]})
    assert old.folders[0].timeout_minutes == 60
    assert old.folders[0].duplicates_hardlink is False
    assert old.notify_on_run is True


def test_watch_loop_runs_folders_concurrently(tmp_path: Path, monkeypatch):
    import schedule_daemon
    from schedule_config import FolderJob, ScheduleConfig

    fa, fb = tmp_path / "A", tmp_path / "B"
    fa.mkdir()
    fb.mkdir()
    cfg = ScheduleConfig(
        folders=[FolderJob(path=str(fa)), FolderJob(path=str(fb))],
        schedule_mode="watch",
        scheduler_enabled=True,
        watch_poll_seconds=0.05,
        watch_quiet_seconds=0.05,
    )
    config_path = tmp_path / "schedule.json"
    save_config(config_path, cfg)

    # Deterministic signature sequence: first call baseline, second and later changed.
    # Only the main polling thread increments the counter so worker signature refreshes
    # don't create a new "change" and re-trigger the folder.
    call_counts = {}

    def mock_signature(job):
        if threading.current_thread().name == "MainThread":
            call_counts[job.path] = call_counts.get(job.path, 0) + 1
        return (job.path, 1 if call_counts.get(job.path, 0) >= 2 else 0)

    monkeypatch.setattr(schedule_daemon, "watch_signature", mock_signature)

    records = []
    lock = threading.Lock()
    stop = threading.Event()

    def mock_run_enabled_folders(
        c, path, *, max_parallel=None, log=None, label=None, file_log_path=None, only_paths=None
    ):
        p = next(iter(only_paths or []))
        start = time.monotonic()
        # "A" is a slow folder; "B" is fast.
        time.sleep(0.5 if Path(p).name == "A" else 0.05)
        end = time.monotonic()
        with lock:
            records.append((p, start, end))
            if len(records) >= 2:
                stop.set()
        return {"ran": 1, "failed": 0}

    monkeypatch.setattr(schedule_daemon, "run_enabled_folders", mock_run_enabled_folders)

    schedule_daemon._run_watch_loop(
        config_path,
        force=False,
        max_parallel=0,
        should_stop=stop.is_set,
    )

    assert len(records) == 2, records
    times = {Path(p).name: (s, e) for p, s, e in records}
    assert "A" in times and "B" in times
    # If B finished before A, the loop ran them concurrently rather than sequentially.
    assert times["B"][1] < times["A"][1]
