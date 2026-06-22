#!/usr/bin/env python3
"""Tests for organize_by_filetype and helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from org_buckets import bucket_for_filename, resolve_profile
from org_exclude import dir_name_excluded, merge_exclude_patterns
from org_organizer import Organizer
from org_paths import normalize_folder_input
from schedule_config import ScheduleConfig, find_path_conflicts


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


def test_schedule_service_launchd_plist_interval(monkeypatch):
    import schedule_service
    from schedule_service import SERVICE_LABEL, build_launchd_plist, daemon_script

    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("interval", "02:00"))
    plist = build_launchd_plist()
    assert plist["Label"] == SERVICE_LABEL
    assert plist["ProgramArguments"][-1] == "--foreground"
    assert str(daemon_script()) in plist["ProgramArguments"]
    assert plist["KeepAlive"] is True
    assert "StartCalendarInterval" not in plist


def test_schedule_service_launchd_plist_daily(monkeypatch):
    import schedule_service
    from schedule_service import SERVICE_LABEL, build_launchd_plist, daemon_script

    monkeypatch.setattr(schedule_service, "_current_schedule", lambda: ("daily", "02:30"))
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
