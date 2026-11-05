"""Regression coverage for intake, configuration ownership and runtime health."""
import copy
import os
from pathlib import Path
import threading
import time

import pytest

import schedule_config as config
import schedule_daemon as daemon
import schedule_runtime as runtime
import schedule_service as service
import schedule_watch as watch


def test_run_completion_preserves_user_pause(tmp_path):
    path = tmp_path / "schedule.json"
    baseline = config.ScheduleConfig(folders=[config.FolderJob(path=str(tmp_path))])
    config.save_config(path, baseline)
    paused = copy.deepcopy(baseline)
    paused.folders[0].enabled = False
    config.save_config(path, paused)
    baseline.folders[0].last_run = "finished"
    config._merge_and_save_config(baseline, path)
    assert not config.load_config(path).folders[0].enabled
    assert config.load_config(path).folders[0].last_run == "finished"


def test_edits_preserve_other_window_changes_and_run_metadata(tmp_path):
    path = tmp_path / "schedule.json"
    baseline = config.ScheduleConfig(scheduler_enabled=True, folders=[config.FolderJob(path="one")])
    config.save_config(path, baseline)
    second = copy.deepcopy(baseline)
    second.scheduler_enabled = False
    second.folders.append(config.FolderJob(path="two"))
    config.merge_config_edits(path, baseline, second)
    baseline.folders[0].last_run = "latest run"
    config._merge_and_save_config(baseline, path)
    edited = copy.deepcopy(baseline)
    edited.folders[0].profile = "extended"
    merged = config.merge_config_edits(path, baseline, edited)
    assert not merged.scheduler_enabled
    assert [job.path for job in merged.folders] == ["one", "two"]
    assert merged.folders[0].last_run == "latest run"
    assert merged.folders[0].profile == "extended"


def test_run_cannot_auto_disable_after_later_settings_edit(tmp_path):
    path = tmp_path / "schedule.json"
    baseline = config.ScheduleConfig(folders=[config.FolderJob(path="one")])
    config.save_config(path, baseline)
    edited = copy.deepcopy(baseline)
    edited.folders[0].timeout_minutes = 120
    fresh = config.merge_config_edits(path, baseline, edited)
    assert fresh.folders[0].settings_revision > 0
    baseline.folders[0].enabled = False
    baseline.folders[0].consecutive_failures = 5
    baseline.folders[0].last_error = "old run failed"
    config._merge_and_save_config(baseline, path)
    restored = config.load_config(path).folders[0]
    assert restored.enabled
    assert restored.consecutive_failures == 0
    assert restored.last_error is None


def test_stale_editor_does_not_resurrect_removed_folder(tmp_path):
    path = tmp_path / "schedule.json"
    baseline = config.ScheduleConfig(folders=[config.FolderJob(path="one")])
    config.save_config(path, baseline)
    config.save_config(path, config.ScheduleConfig())
    edited = copy.deepcopy(baseline)
    edited.folders[0].profile = "extended"
    assert not config.merge_config_edits(path, baseline, edited).folders


@pytest.mark.parametrize("depth", [0, 2])
def test_polling_signature_sees_content_growth(tmp_path, depth):
    root = tmp_path / "root"
    folder = root.joinpath(*(["nested"] * depth))
    folder.mkdir(parents=True)
    file = folder / "video.mp4"
    file.write_bytes(b"part")
    job = config.FolderJob(path=str(root))
    before = config.watch_signature(job)
    before_fast = config.watch_signature_fast(job)
    with file.open("ab") as stream:
        stream.write(b"more content")
        stream.flush()
        os.fsync(stream.fileno())
    assert config.watch_signature(job) != before
    if depth == 0:
        assert config.watch_signature_fast(job) != before_fast


def test_readiness_defers_growing_and_partial_files(tmp_path, monkeypatch):
    changing = tmp_path / "video.mp4"
    stable = tmp_path / "ready.jpg"
    partial = tmp_path / "video.crdownload"
    package = tmp_path / "other.download"
    package.mkdir()
    contained = package / "movie.mp4"
    for path in (changing, stable, partial, contained):
        path.write_bytes(b"start")
    real_snapshot = watch._candidate_snapshot
    calls = 0

    def snapshot(job, paths):
        nonlocal calls
        calls += 1
        if calls == 2:
            changing.write_bytes(b"growing")
        return real_snapshot(job, paths)

    monkeypatch.setattr(watch, "_candidate_snapshot", snapshot)
    deferred = watch.readiness_deferred(config.FolderJob(path=str(tmp_path)), stable_seconds=0)
    assert deferred == {str(changing), str(partial), str(contained)}
    assert stable.exists()


def _fake_monitor(root):
    monitor = object.__new__(watch.WatchEventMonitor)
    monitor._lock = threading.Lock()
    monitor._roots = {str(root): str(root)}
    monitor._changes = {}
    monitor._on_dirty = lambda value: None
    monitor.backend_name = lambda: "test"
    monitor.set_watched_paths = lambda paths: None
    monitor.stop = lambda: None
    return monitor


def test_cleanup_can_use_directory_event_batch(tmp_path):
    monitor = _fake_monitor(tmp_path)
    folder = tmp_path / "empty"
    monitor._dispatch(str(folder), is_directory=True)
    assert monitor.take_changes(str(tmp_path), directories=True) == {str(folder)}
    for index in range(520):
        monitor._dispatch(str(tmp_path / str(index)), is_directory=True)
    assert monitor.take_changes(str(tmp_path), directories=True) is None


@pytest.mark.parametrize("options", [dict(), dict(strategy="in-place", random_names_after_organize=True),
                                     dict(empty_dirs_only=True)])
def test_startup_reconciles_then_delta_avoids_post_run_scan(tmp_path, monkeypatch, options):
    root = tmp_path / "root"
    root.mkdir()
    path = str(root)
    cfg = config.ScheduleConfig(schedule_mode="watch", scheduler_enabled=True,
                                watch_quiet_seconds=0.01,
                                folders=[config.FolderJob(path=path, dry_run_verified=True, **options)])
    config_path = tmp_path / "schedule.json"
    config.save_config(config_path, cfg)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(daemon, "WATCH_EVENT_TICK_SECONDS", 0.01)
    monkeypatch.setattr(daemon, "_notify_run", lambda *args: None)
    monkeypatch.setattr(daemon, "readiness_deferred", lambda *args, **kwargs: set())
    monitor = _fake_monitor(root)

    def create(callback):
        monitor._on_dirty = callback
        return monitor

    monkeypatch.setattr(daemon, "create_event_monitor", create)
    calls = []
    post_run_scans = []
    stop = threading.Event()

    def signature(job):
        if threading.current_thread().name.startswith("watch-organize"):
            post_run_scans.append(len(calls))
        return (1,)

    monkeypatch.setattr(daemon, "watch_signature", signature)

    def run(*args, **kwargs):
        calls.append(kwargs.get("changed_paths"))
        if len(calls) == 1:
            monitor._dispatch(str(root / "new.jpg"), is_directory=False)
        else:
            stop.set()
        return {"ran": 1, "failed": 0}

    monkeypatch.setattr(daemon, "run_enabled_folders", run)
    deadline = time.monotonic() + 3
    daemon._run_watch_loop(config_path, force=False, max_parallel=1,
                           should_stop=lambda: stop.is_set() or time.monotonic() > deadline)
    assert calls == [None, {path: {str(root / "new.jpg")}}]
    assert post_run_scans == [1]


def test_runtime_install_selects_only_validated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_ORG_RUNTIME_HOME", str(tmp_path / "runtime"))

    class Builder:
        def __init__(self, **kwargs):
            assert kwargs["symlinks"] is False

        def create(self, destination):
            (destination / "bin").mkdir(parents=True)
            (destination / "bin/python").touch()

    monkeypatch.setattr(runtime.venv, "EnvBuilder", Builder)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_probe", lambda path: {"watchdog": "test", "version": [3, 14, 0]})
    result = runtime.install_runtime()
    assert runtime.managed_python() == Path(result["python"])
    previous = (runtime.runtime_root() / "current.json").read_text()
    monkeypatch.setattr(runtime, "_probe", lambda path: {})
    with pytest.raises(RuntimeError):
        runtime.install_runtime()
    assert (runtime.runtime_root() / "current.json").read_text() == previous


def test_managed_runtime_reexec_preserves_daemon_arguments(tmp_path, monkeypatch):
    managed = tmp_path / "runtime/bin/python"
    monkeypatch.setattr(runtime, "managed_python", lambda: managed)
    monkeypatch.delenv("FILE_ORG_RUNTIME_ACTIVE", raising=False)
    monkeypatch.setattr(runtime.sys, "argv", ["schedule_daemon.py", "--foreground"])
    calls = []
    monkeypatch.setattr(runtime.os, "execve", lambda *args: calls.append(args))
    runtime.activate_managed_runtime()
    assert calls[0][1] == [str(managed), "schedule_daemon.py", "--foreground"]
    assert calls[0][2]["FILE_ORG_RUNTIME_ACTIVE"] == "1"


def test_refresh_preserves_permission_bearing_wrapper(tmp_path, monkeypatch):
    import plistlib

    wrapper = tmp_path / "Permission.app/Contents/MacOS/Permission"
    wrapper.parent.mkdir(parents=True)
    wrapper.touch()
    plist = tmp_path / "service.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(wrapper)]}))
    monkeypatch.setattr(service, "launchd_plist_path", lambda: plist)
    assert service._preserve_app_wrapper(["python", "script"]) == [str(wrapper)]


def test_pause_during_readiness_prevents_launch(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    job = config.FolderJob(path=str(root), dry_run_verified=True)
    cfg = config.ScheduleConfig(schedule_mode="watch", scheduler_enabled=True,
                                watch_quiet_seconds=0, folders=[job])
    path = tmp_path / "schedule.json"
    config.save_config(path, cfg)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(daemon, "create_event_monitor", lambda callback: None)
    stop = threading.Event()

    def pause(*args, **kwargs):
        paused = config.load_config(path)
        paused.scheduler_enabled = False
        config.save_config(path, paused)
        stop.set()
        return set()

    monkeypatch.setattr(daemon, "readiness_deferred", pause)
    monkeypatch.setattr(daemon, "run_enabled_folders", lambda *args, **kwargs: pytest.fail("launched after pause"))
    deadline = time.monotonic() + 2
    daemon._run_watch_loop(path, force=False, max_parallel=1,
                           should_stop=lambda: stop.is_set() or time.monotonic() > deadline)
    assert stop.is_set()
    assert not config.load_config(path).scheduler_enabled


def test_settings_signature_detects_rule_contents(tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text('{"rules": []}')
    job = config.FolderJob(path=str(tmp_path), rules_file=str(rules))
    before = daemon._job_settings_signature(job)
    rules.write_text('{"rules": [{"id":"new"}]}')
    assert daemon._job_settings_signature(job) != before


def test_watch_snapshot_honors_hidden_excluded_and_safety_trees(tmp_path):
    for directory in (".hidden", "skip", "For Deletion", "Needs Review", "Images"):
        target = tmp_path / directory
        target.mkdir()
        (target / "file.jpg").touch()
    job = config.FolderJob(path=str(tmp_path), include_hidden=False, exclude=["skip"])
    snapshot = watch.watch_snapshot(job)
    assert str(tmp_path / "Images/file.jpg") in snapshot
    for directory in (".hidden", "skip", "For Deletion", "Needs Review"):
        assert str(tmp_path / directory / "file.jpg") not in snapshot


def test_wrapped_daily_service_still_requests_one_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_log_paths", lambda: (tmp_path / "out", tmp_path / "err"))
    monkeypatch.setattr(service, "_current_schedule", lambda: ("daily", "06:00", 60))
    monkeypatch.setattr(service, "_python_executable", lambda: "python")
    monkeypatch.setattr(service, "_preserve_app_wrapper", lambda args: ["/Applications/Permission.app/Contents/MacOS/Permission"])
    plist = service.build_launchd_plist()
    assert plist["ProgramArguments"] == ["/Applications/Permission.app/Contents/MacOS/Permission"]
    assert plist["EnvironmentVariables"]["FILE_ORG_DAEMON_ONCE"] == "1"
    assert "KeepAlive" not in plist


def _receipt(path):
    import stat
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "directory": stat.S_ISDIR(info.st_mode)}


def test_own_receipts_filter_queued_and_delayed_events_but_keep_edits(tmp_path):
    bucket = tmp_path / "Images"
    bucket.mkdir()
    own = bucket / "renamed.jpg"
    own.write_bytes(b"own")
    monitor = _fake_monitor(tmp_path)
    # Events can arrive while the organizer is still running, before receipts.
    monitor._dispatch(str(bucket), is_directory=True)
    monitor._dispatch(str(own))
    monitor.acknowledge_mutations({str(bucket): _receipt(bucket), str(own): _receipt(own)})
    assert monitor.take_changes(str(tmp_path)) == set()
    hits = []
    monitor._on_dirty = hits.append
    monitor._dispatch(str(own))
    assert hits == []  # delayed identical event is also suppressed
    own.write_bytes(b"edit")
    monitor._dispatch(str(own))
    assert hits == [str(tmp_path)]
    assert monitor.take_changes(str(tmp_path)) == {str(own)}


def test_deleted_source_receipt_does_not_hide_new_arrival(tmp_path):
    source = tmp_path / "source.jpg"
    monitor = _fake_monitor(tmp_path)
    monitor.acknowledge_mutations({str(source): None})
    monitor._dispatch(str(source))
    assert monitor.take_changes(str(tmp_path)) is None
    source.write_bytes(b"new arrival")
    monitor._dispatch(str(source))
    assert monitor.take_changes(str(tmp_path)) == {str(source)}


def test_watch_random_naming_without_skip_does_not_loop_on_own_events(tmp_path, monkeypatch):
    from org_organizer import Organizer

    root = tmp_path / "root"
    root.mkdir()
    (root / "photo.jpg").write_bytes(b"photo")
    path = str(root)
    cfg = config.ScheduleConfig(schedule_mode="watch", scheduler_enabled=True,
                                watch_quiet_seconds=0.01, folders=[config.FolderJob(
                                    path=path, dry_run_verified=True,
                                    random_names_after_organize=True, skip_randomly_renamed=False)])
    config_path = tmp_path / "schedule.json"
    config.save_config(config_path, cfg)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(daemon, "WATCH_EVENT_TICK_SECONDS", 0.01)
    monkeypatch.setattr(daemon, "_notify_run", lambda *args: None)
    monkeypatch.setattr(daemon, "readiness_deferred", lambda *args, **kwargs: set())
    monitor = _fake_monitor(root)

    def create(callback):
        monitor._on_dirty = callback
        return monitor

    monkeypatch.setattr(daemon, "create_event_monitor", create)
    runs = []
    finished_at = []

    def run(*args, **kwargs):
        changes = kwargs.get("changed_paths", {}).get(path)
        result = Organizer(root, recursive=True, strategy="flatten-root", include_hidden=False,
                           normalize="none", collect_empty_dirs=False, dry_run=False,
                           random_names_after_organize=True, skip_randomly_renamed=False,
                           changed_paths=changes).run()
        runs.append(result)
        # Queue file and directory events before the daemon receives receipts.
        for changed, identity in result["watch_mutations"].items():
            monitor._dispatch(changed, is_directory=bool(identity and identity["directory"]))
        finished_at.append(time.monotonic())
        return {"ran": 1, "failed": 0, "watch_mutations": result["watch_mutations"]}

    monkeypatch.setattr(daemon, "run_enabled_folders", run)
    deadline = time.monotonic() + 5
    daemon._run_watch_loop(config_path, force=False, max_parallel=1,
                           should_stop=lambda: time.monotonic() > deadline or len(runs) > 1
                           or bool(finished_at and time.monotonic() - finished_at[0] > 0.25))
    assert len(runs) == 1
    assert runs[0]["rename_after_organize"]["files_renamed"] == 1
