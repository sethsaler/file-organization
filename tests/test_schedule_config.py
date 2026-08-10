#!/usr/bin/env python3
"""Tests for schedule_config (schema, load/save, normalization, helpers)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from schedule_config import (
    CONFIG_VERSION,
    DEFAULT_DAILY_TIME,
    SCHEDULE_MODE_DAILY,
    SCHEDULE_MODE_INTERVAL,
    SCHEDULE_MODE_WATCH,
    WATCH_POLL_SECONDS,
    WATCH_QUIET_SECONDS,
    FolderJob,
    ScheduleConfig,
    _effective_max_workers,
    build_organize_cmd,
    count_unsorted_files,
    default_config_path,
    effective_normalize,
    expand_subfolders,
    find_path_conflicts,
    format_daily_time,
    load_config,
    normalize_daily_time,
    normalize_schedule_mode,
    parse_daily_time,
    save_config,
    seconds_until_next_daily_run,
    wait_seconds_after_run,
)


def test_schedule_config_defaults():
    cfg = ScheduleConfig()
    assert cfg.version == CONFIG_VERSION
    assert cfg.schedule_mode == SCHEDULE_MODE_INTERVAL
    assert cfg.interval_minutes == 60
    assert cfg.daily_time == DEFAULT_DAILY_TIME
    assert cfg.scheduler_enabled is False
    assert cfg.max_parallel == 0
    assert cfg.max_failures_before_disable == 5
    assert cfg.notify_on_run is True
    assert cfg.watch_poll_seconds == WATCH_POLL_SECONDS
    assert cfg.watch_quiet_seconds == WATCH_QUIET_SECONDS
    assert cfg.folders == []


def test_folder_job_defaults():
    job = FolderJob(path="/tmp/x")
    assert job.enabled is True
    assert job.recursive is True
    assert job.strategy == "flatten-root"
    assert job.normalize is None
    assert job.include_hidden is True
    assert job.collect_empty_dirs is True
    assert job.profile == "standard"
    assert job.exclude_defaults is True
    assert job.exclude == []
    assert job.consecutive_failures == 0
    assert job.dry_run_verified is False
    assert job.last_run is None
    assert job.last_error is None
    assert job.expand_subfolders is False
    assert job.random_names_after_organize is False
    assert job.skip_randomly_renamed is True
    assert job.min_unsorted_threshold == 0
    assert job.detect_duplicates is False
    assert job.duplicates_hardlink is False
    assert job.date_buckets is False
    assert job.rules_file is None
    assert job.unmatched_mode == "bucket"
    assert job.archive_root is None
    assert job.archive_mapping is None
    assert job.timeout_minutes == 60


def test_normalize_schedule_mode():
    assert normalize_schedule_mode("daily") == SCHEDULE_MODE_DAILY
    assert normalize_schedule_mode("DAILY") == SCHEDULE_MODE_DAILY
    assert normalize_schedule_mode(" watch ") == SCHEDULE_MODE_WATCH
    assert normalize_schedule_mode("interval") == SCHEDULE_MODE_INTERVAL
    assert normalize_schedule_mode("") == SCHEDULE_MODE_INTERVAL
    assert normalize_schedule_mode("bogus") == SCHEDULE_MODE_INTERVAL


def test_parse_daily_time_valid_and_clamped():
    assert parse_daily_time("07:05") == (7, 5)
    assert parse_daily_time("7:5") == (7, 5)
    assert parse_daily_time("23:59") == (23, 59)
    # Out-of-range values clamp instead of raising.
    assert parse_daily_time("25:99") == (23, 59)
    assert parse_daily_time("-3:-9") == (0, 0)


def test_parse_daily_time_invalid_defaults_to_midnight():
    assert parse_daily_time("garbage") == (0, 0)
    assert parse_daily_time("12") == (0, 0)
    assert parse_daily_time("1:2:3") == (0, 0)
    assert parse_daily_time("") == (0, 0)


def test_normalize_daily_time_formats_and_defaults():
    assert format_daily_time(7, 5) == "07:05"
    assert normalize_daily_time("7:5") == "07:05"
    assert normalize_daily_time("25:99") == "23:59"
    assert normalize_daily_time("garbage") == "00:00"
    assert normalize_daily_time("") == "00:00"


def test_seconds_until_next_daily_run():
    now = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert seconds_until_next_daily_run("10:30", now=now) == pytest.approx(1800.0)
    # Target already passed today: rolls to tomorrow.
    assert seconds_until_next_daily_run("09:59", now=now) == pytest.approx(86340.0)
    # Exactly now counts as passed (target <= now).
    assert seconds_until_next_daily_run("10:00", now=now) == pytest.approx(86400.0)


def test_wait_seconds_after_run_interval_clamps():
    cfg = ScheduleConfig(interval_minutes=90)
    assert wait_seconds_after_run(cfg) == pytest.approx(5400.0)
    cfg.interval_minutes = 0
    assert wait_seconds_after_run(cfg) == pytest.approx(60.0)
    cfg.interval_minutes = 99999
    assert wait_seconds_after_run(cfg) == pytest.approx(10080 * 60.0)


def test_wait_seconds_after_run_watch_and_daily():
    cfg = ScheduleConfig(schedule_mode="watch", watch_poll_seconds=1.25)
    assert wait_seconds_after_run(cfg) == pytest.approx(1.25)
    cfg = ScheduleConfig(schedule_mode="daily", daily_time="06:30")
    secs = wait_seconds_after_run(cfg)
    assert 0.0 <= secs <= 86400.0


def test_to_json_dict_conditional_keys():
    d = ScheduleConfig().to_json_dict()
    assert d["version"] == CONFIG_VERSION
    assert d["schedule_mode"] == SCHEDULE_MODE_INTERVAL
    assert "daily_time" not in d
    assert "max_parallel" not in d
    assert "max_failures_before_disable" not in d
    assert "notify_on_run" not in d
    assert "watch_poll_seconds" not in d

    cfg = ScheduleConfig(
        schedule_mode="daily",
        daily_time="7:5",
        max_parallel=4,
        max_failures_before_disable=7,
        notify_on_run=False,
        watch_poll_seconds=1.5,
        watch_quiet_seconds=2.5,
    )
    d = cfg.to_json_dict()
    assert d["schedule_mode"] == SCHEDULE_MODE_DAILY
    assert d["daily_time"] == "07:05"
    assert d["max_parallel"] == 4
    assert d["max_failures_before_disable"] == 7
    assert d["notify_on_run"] is False
    assert d["watch_poll_seconds"] == 1.5
    assert d["watch_quiet_seconds"] == 2.5


def test_save_load_round_trip(tmp_path: Path):
    cfg = ScheduleConfig(
        schedule_mode="daily",
        daily_time="06:30",
        interval_minutes=120,
        scheduler_enabled=True,
        max_parallel=3,
        notify_on_run=False,
        folders=[
            FolderJob(
                path=str(tmp_path),
                enabled=False,
                recursive=False,
                strategy="in-place",
                profile="extended",
                exclude=["*.tmp", "cache"],
                detect_duplicates=True,
                rules_file="rules.json",
                unmatched_mode="needs-review",
                archive_root=str(tmp_path / "arch"),
                timeout_minutes=30,
                min_unsorted_threshold=5,
                last_run="2026-01-01T00:00:00+00:00",
            )
        ],
    )
    path = tmp_path / "nested" / "schedule.json"
    save_config(path, cfg)
    assert path.is_file()
    # Atomic write leaves no temp file behind.
    assert list(path.parent.iterdir()) == [path]

    loaded = load_config(path)
    assert loaded.to_json_dict() == cfg.to_json_dict()
    job = loaded.folders[0]
    assert job.enabled is False
    assert job.recursive is False
    assert job.strategy == "in-place"
    assert job.exclude == ["*.tmp", "cache"]
    assert job.unmatched_mode == "needs-review"
    assert job.timeout_minutes == 30
    assert job.last_run == "2026-01-01T00:00:00+00:00"


def test_load_config_missing_or_invalid_returns_defaults(tmp_path: Path):
    defaults = ScheduleConfig().to_json_dict()
    assert load_config(tmp_path / "missing.json").to_json_dict() == defaults

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_config(bad).to_json_dict() == defaults

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config(wrong_shape).to_json_dict() == defaults


def test_from_json_dict_clamps_values():
    cfg = ScheduleConfig.from_json_dict(
        {
            "interval_minutes": 0,
            "max_parallel": 999,
            "max_failures_before_disable": -2,
            "watch_poll_seconds": 0.01,
            "watch_quiet_seconds": -1.0,
            "daily_time": "7:5",
        }
    )
    assert cfg.interval_minutes == 1
    assert cfg.max_parallel == 128
    assert cfg.max_failures_before_disable == 0
    assert cfg.watch_poll_seconds == pytest.approx(0.05)
    assert cfg.watch_quiet_seconds == pytest.approx(0.0)
    assert cfg.daily_time == "07:05"

    cfg = ScheduleConfig.from_json_dict({"interval_minutes": 99999, "max_parallel": -1})
    assert cfg.interval_minutes == 10080
    assert cfg.max_parallel == 0


def test_from_json_dict_folder_parsing_edge_cases(tmp_path: Path):
    p = str(tmp_path)
    cfg = ScheduleConfig.from_json_dict(
        {
            "version": 6,
            "folders": [
                {"path": ""},  # empty path skipped
                {"path": "   "},  # blank path skipped
                "not-a-dict",  # non-dict skipped
                {
                    "path": p,
                    "exclude": "not-a-list",
                    "normalize": "   ",
                    "timeout_minutes": 9999,
                    "min_unsorted_threshold": -5,
                    "unmatched_mode": "bogus",
                    "rules_file": "   ",
                },
            ],
        }
    )
    assert len(cfg.folders) == 1
    job = cfg.folders[0]
    assert job.path == p
    assert job.exclude == []
    assert job.normalize is None
    assert job.timeout_minutes == 1440
    assert job.min_unsorted_threshold == 0
    assert job.unmatched_mode == "bucket"
    assert job.rules_file is None
    assert job.dry_run_verified is False


def test_from_json_dict_normalize_and_legacy_dry_run(tmp_path: Path):
    p = str(tmp_path)
    # Non-recursive + standard normalize collapses to None.
    cfg = ScheduleConfig.from_json_dict(
        {"version": 6, "folders": [{"path": p, "recursive": False, "normalize": "standard"}]}
    )
    assert cfg.folders[0].normalize is None

    # Pre-v4 configs were treated as dry-run verified.
    cfg = ScheduleConfig.from_json_dict({"version": 3, "folders": [{"path": p}]})
    assert cfg.folders[0].dry_run_verified is True

    # v4+ respects the stored flag.
    cfg = ScheduleConfig.from_json_dict(
        {"version": 6, "folders": [{"path": p, "dry_run_verified": True}]}
    )
    assert cfg.folders[0].dry_run_verified is True

    # Valid fallback modes survive (casefolded).
    cfg = ScheduleConfig.from_json_dict(
        {"version": 6, "folders": [{"path": p, "unmatched_mode": "NEEDS-REVIEW"}]}
    )
    assert cfg.folders[0].unmatched_mode == "needs-review"


def test_effective_normalize():
    assert effective_normalize(FolderJob(path="/x")) == "standard"
    assert effective_normalize(FolderJob(path="/x", recursive=False)) == "none"
    assert effective_normalize(FolderJob(path="/x", normalize="none")) == "none"
    assert effective_normalize(FolderJob(path="/x", recursive=False, normalize="standard")) == "standard"


def test_find_path_conflicts(tmp_path: Path):
    parent = tmp_path / "p"
    child = parent / "c"
    child.mkdir(parents=True)

    cfg = ScheduleConfig(
        folders=[FolderJob(path=str(parent)), FolderJob(path=str(child))]
    )
    warnings = find_path_conflicts(cfg)
    assert len(warnings) == 1
    assert "Nested paths" in warnings[0]

    # Disabled folders are ignored.
    cfg.folders[1].enabled = False
    assert find_path_conflicts(cfg) == []

    # Disjoint paths produce no warnings.
    other = tmp_path / "other"
    other.mkdir()
    cfg = ScheduleConfig(folders=[FolderJob(path=str(parent)), FolderJob(path=str(other))])
    assert find_path_conflicts(cfg) == []


def test_expand_subfolders(tmp_path: Path):
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()
    (tmp_path / "file.txt").write_text("x")

    job = FolderJob(
        path=str(tmp_path),
        expand_subfolders=True,
        recursive=False,
        profile="extended",
        dry_run_verified=True,
    )
    expanded = expand_subfolders(job)
    assert len(expanded) == 2
    paths = {Path(j.path).name for j in expanded}
    assert paths == {"d1", "d2"}
    for sub in expanded:
        assert sub.expand_subfolders is False
        assert sub.recursive is False
        assert sub.profile == "extended"
        assert sub.dry_run_verified is True

    # Disabled: returned unchanged.
    assert expand_subfolders(FolderJob(path=str(tmp_path))) == [FolderJob(path=str(tmp_path))]

    # No subfolders or missing dir: returned unchanged.
    empty = tmp_path / "empty"
    empty.mkdir()
    solo = FolderJob(path=str(empty), expand_subfolders=True)
    assert expand_subfolders(solo) == [solo]
    gone = FolderJob(path=str(tmp_path / "missing"), expand_subfolders=True)
    assert expand_subfolders(gone) == [gone]


def _make_tree(base: Path) -> None:
    (base / "root.txt").write_text("a")
    (base / "other.bin").write_bytes(b"\x00")
    (base / ".DS_Store").write_bytes(b"\x00")
    sub = base / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("b")


def test_count_unsorted_files_flatten_root_counts_root_only(tmp_path: Path):
    _make_tree(tmp_path)
    job = FolderJob(path=str(tmp_path))
    assert count_unsorted_files(job) == 2
    # stop_at caps the scan.
    assert count_unsorted_files(job, stop_at=1) == 1


def test_count_unsorted_files_in_place_skips_buckets(tmp_path: Path):
    _make_tree(tmp_path)
    bucket = tmp_path / "Images"
    bucket.mkdir()
    (bucket / "sorted.jpg").write_bytes(b"\xff")
    deletion = tmp_path / "For Deletion"
    deletion.mkdir()
    (deletion / "junk.txt").write_text("j")
    organizer = tmp_path / ".organizer"
    organizer.mkdir()
    (organizer / "backup.json").write_text("{}")

    job = FolderJob(path=str(tmp_path), strategy="in-place", recursive=True)
    # root.txt, other.bin, sub/nested.txt — bucket/organizer dirs skipped.
    assert count_unsorted_files(job) == 3


def test_count_unsorted_files_missing_dir(tmp_path: Path):
    assert count_unsorted_files(FolderJob(path=str(tmp_path / "nope"))) == 0


def test_effective_max_workers():
    assert _effective_max_workers(0, 0) == 1
    assert _effective_max_workers(5, 2) == 2
    assert _effective_max_workers(5, 0) == 5
    assert _effective_max_workers(100, 0) == 32
    assert _effective_max_workers(200, 200) == 128


def test_build_organize_cmd_defaults_and_dry_run(tmp_path: Path):
    job = FolderJob(path=str(tmp_path))
    cmd = build_organize_cmd(job, "/usr/bin/python3")
    assert cmd[0] == "/usr/bin/python3"
    assert "--path" in cmd and str(Path(str(tmp_path))) in cmd
    assert "--recursive" in cmd
    assert "--strategy" in cmd
    assert cmd[cmd.index("--strategy") + 1] == "flatten-root"
    assert cmd[cmd.index("--normalize") + 1] == "standard"
    assert cmd[cmd.index("--profile") + 1] == "standard"
    assert "--backup" in cmd
    assert "--dry-run" not in cmd
    assert "--dry-run" in build_organize_cmd(job, dry_run=True)


def test_build_organize_cmd_optional_flags(tmp_path: Path):
    job = FolderJob(
        path=str(tmp_path),
        recursive=False,
        include_hidden=False,
        collect_empty_dirs=False,
        exclude_defaults=False,
        exclude=["*.tmp", "  ", "cache"],
        random_names_after_organize=True,
        detect_duplicates=True,
        duplicates_hardlink=True,
        date_buckets=True,
        unmatched_mode="needs-review",
    )
    cmd = build_organize_cmd(job)
    assert "--no-recursive" in cmd
    assert "--no-include-hidden" in cmd
    assert "--no-collect-empty-dirs" in cmd
    assert "--exclude-defaults" not in cmd
    # Blank patterns are dropped.
    assert cmd.count("--exclude") == 2
    assert "--random-names-after-organize" in cmd
    assert "--detect-duplicates" in cmd
    assert "--duplicates-hardlink" in cmd
    assert "--date-buckets" in cmd
    assert cmd[cmd.index("--unmatched") + 1] == "needs-review"


def test_build_organize_cmd_rules_and_archive(tmp_path: Path):
    rules = tmp_path / "rules.json"
    rules.write_text("{}", encoding="utf-8")

    job = FolderJob(path=str(tmp_path), rules_file=str(rules))
    cmd = build_organize_cmd(job)
    assert cmd[cmd.index("--rules") + 1] == str(rules)

    # archive_root wins over rules; mapping only follows archive_root.
    job = FolderJob(
        path=str(tmp_path),
        rules_file=str(rules),
        archive_root=str(tmp_path / "arch"),
        archive_mapping=str(tmp_path / "map.json"),
    )
    cmd = build_organize_cmd(job)
    assert "--rules" not in cmd
    assert cmd[cmd.index("--archive-root") + 1] == str(tmp_path / "arch")
    assert cmd[cmd.index("--archive-mapping") + 1] == str(tmp_path / "map.json")


def test_default_config_path_respects_xdg(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "file-organization" / "schedule.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", "   ")
    assert default_config_path() == Path.home() / ".config" / "file-organization" / "schedule.json"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert default_config_path() == Path.home() / ".config" / "file-organization" / "schedule.json"


def test_saved_json_is_valid_and_versioned(tmp_path: Path):
    path = tmp_path / "schedule.json"
    save_config(path, ScheduleConfig())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == CONFIG_VERSION
    assert data["folders"] == []
