"""Behavior and work-avoidance checks for incremental organization."""
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import org_dupes
import org_organizer
import schedule_daemon
import schedule_watch
from org_dupes import HashCache
from org_manifest import restore_from_manifest
from org_organizer import Organizer
from org_rules import RuleSet
from schedule_config import FolderJob, ScheduleConfig, save_config


def organizer(root, **options):
    defaults = dict(base=root, recursive=True, strategy="flatten-root", include_hidden=False,
                    normalize="standard", collect_empty_dirs=True, dry_run=False)
    defaults.update(options)
    return Organizer(**defaults)


def test_hash_cache_reuses_and_invalidates_same_size_same_mtime(tmp_path, monkeypatch):
    path = tmp_path / "a"
    path.write_bytes(b"abcd")
    cache_path = tmp_path / "cache.json"
    cache = HashCache(cache_path)
    first = cache.get(path)
    cache.save()
    cached = HashCache(cache_path)
    real_hash = org_dupes.hash_file
    calls = []
    monkeypatch.setattr(org_dupes, "hash_file", lambda p: calls.append(p) or real_hash(p))
    assert cached.get(path) == first
    assert calls == []
    mtime = path.stat().st_mtime_ns
    path.write_bytes(b"wxyz")
    os.utime(path, ns=(mtime, mtime))
    assert cached.get(path) != first  # ctime catches same-size/same-mtime edits
    assert calls == [path]


def test_hash_cache_rejects_file_changed_during_hash(tmp_path, monkeypatch):
    path = tmp_path / "a"
    path.write_bytes(b"first")
    real_hash = org_dupes.hash_file

    def changing_hash(p):
        digest = real_hash(p)
        p.write_bytes(b"second")
        return digest

    monkeypatch.setattr(org_dupes, "hash_file", changing_hash)
    cache = HashCache(tmp_path / "cache")
    assert cache.get(path) is None
    assert cache.entries == {}


@pytest.mark.parametrize("data", ["not json", "[]", '{"version":1,"entries":{"a":{}}}'])
def test_bad_cache_is_disposable(tmp_path, data):
    path = tmp_path / "cache"
    path.write_text(data)
    assert HashCache(path).entries == {}


def test_hash_cache_move_and_noop_save(tmp_path, monkeypatch):
    path = tmp_path / "a"
    path.write_bytes(b"content")
    cache_path = tmp_path / "cache"
    cache = HashCache(cache_path)
    digest = cache.get(path)
    target = tmp_path / "b"
    path.rename(target)
    cache.moved(path, target)
    cache.save()
    mtime = cache_path.stat().st_mtime_ns
    loaded = HashCache(cache_path)
    monkeypatch.setattr(org_dupes, "hash_file", lambda p: pytest.fail("cache miss after rename"))
    assert loaded.get(target) == digest
    loaded.save()
    assert cache_path.stat().st_mtime_ns == mtime


def test_hash_cache_does_not_read_placeholders(tmp_path, monkeypatch):
    cache = HashCache(tmp_path / "cache")
    monkeypatch.setattr(org_dupes, "stat_is_dataless", lambda st: True)
    monkeypatch.setattr(org_dupes, "hash_file", lambda p: pytest.fail("read cloud placeholder"))
    path = tmp_path / "cloud"
    path.write_bytes(b"cloud")
    assert cache.get(path) is None


def test_incremental_moves_only_selected_files_and_restores(tmp_path, monkeypatch):
    root = tmp_path / "root"
    inbox = root / "inbox"
    inbox.mkdir(parents=True)
    source = inbox / "photo.jpg"
    source.write_bytes(b"photo")
    (root / "untouched.txt").write_bytes(b"stay")
    run = organizer(root, changed_paths=[source])
    monkeypatch.setattr(run, "_walk_topdown_organize", lambda: pytest.fail("full organize walk"))
    result = run.run()
    assert result["execution"] == "incremental"
    assert result["files_moved"] == 1
    assert (root / "untouched.txt").exists()
    assert (root / "Images/photo.jpg").read_bytes() == b"photo"
    assert result["phase_seconds"]["total"] >= result["phase_seconds"]["organize"]
    assert restore_from_manifest(result["backup_manifest"])
    assert source.read_bytes() == b"photo"


def test_incremental_rejects_outside_hidden_excluded_and_symlink_paths(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"external")
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "secret.jpg").write_bytes(b"secret")
    excluded = root / "skip"
    excluded.mkdir()
    (excluded / "skip.jpg").write_bytes(b"skip")
    run = organizer(root, changed_paths=[outside, root / "link/outside.jpg", hidden / "secret.jpg", excluded / "skip.jpg"],
                    exclude_patterns=["skip"])
    assert run.run()["files_moved"] == 0
    assert outside.exists() and (hidden / "secret.jpg").exists() and (excluded / "skip.jpg").exists()


@pytest.mark.parametrize("options", [dict(dry_run=True), dict(rule_set=RuleSet()),
                                     dict(strategy="in-place"), dict(random_names_after_organize=True)])
def test_incremental_falls_back_for_global_operations(tmp_path, options):
    source = tmp_path / "a.jpg"
    source.write_bytes(b"photo")
    assert organizer(tmp_path, changed_paths=[source], **options).run()["execution"] == "full"


def test_incremental_duplicate_preserves_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "root"
    (root / "Images").mkdir(parents=True)
    original = root / "Images/original.jpg"
    original.write_bytes(b"same")
    incoming = root / "incoming.jpg"
    incoming.write_bytes(b"same")
    result = organizer(root, detect_duplicates=True, changed_paths=[incoming]).run()
    assert result["duplicates"]["files_moved"] == 1
    assert original.exists() and (root / "Duplicates/incoming.jpg").exists()
    (root / "another.jpg").write_bytes(b"same")
    again = organizer(root, detect_duplicates=True).run()
    assert again["hash_cache"]["hits"] > 0


def test_cleanup_reuses_walk_inventory_and_preserves_exclusions(tmp_path, monkeypatch):
    (tmp_path / "keep/empty").mkdir(parents=True)
    (tmp_path / "remove").mkdir()
    run = organizer(tmp_path, collect_empty_dirs=False, exclude_patterns=["keep"])
    run._cleanup_dsstore_files()
    monkeypatch.setattr(org_organizer.os, "walk", lambda *a, **k: pytest.fail("second cleanup walk"))
    run._remove_empty_subdirs()
    assert not (tmp_path / "remove").exists()
    assert (tmp_path / "keep/empty").exists()


def test_collection_discovers_whole_tree_once(tmp_path, monkeypatch):
    (tmp_path / "a/b/c").mkdir(parents=True)
    (tmp_path / "stay.txt").write_text("stay")
    run = organizer(tmp_path)
    find = run._find_empty_dir_candidates
    calls = []
    monkeypatch.setattr(run, "_find_empty_dir_candidates", lambda: calls.append(1) or find())
    run.run()
    assert calls == [1]
    assert (tmp_path / "For Deletion/a/b/c").is_dir()


def monitor_without_observer(root):
    monitor = object.__new__(schedule_watch.WatchEventMonitor)
    monitor._lock = threading.Lock()
    monitor._roots = {str(root): str(root)}
    monitor._changes = {}
    monitor._on_dirty = lambda path: None
    return monitor


def test_move_events_include_both_roots(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    monitor = monitor_without_observer(a)
    monitor._roots[str(b)] = str(b)
    handler = schedule_watch._DirtyHandler(monitor)
    handler.on_any_event(SimpleNamespace(event_type="moved", is_directory=False,
                                        src_path=str(a / "old.jpg"), dest_path=str(b / "new.jpg")))
    assert monitor.take_changes(str(a)) == {str(a / "old.jpg")}
    assert monitor.take_changes(str(b)) == {str(b / "new.jpg")}


def test_directory_and_overflow_events_request_full_run(tmp_path):
    monitor = monitor_without_observer(tmp_path)
    monitor._dispatch(str(tmp_path / "sub"), is_directory=True)
    assert monitor.take_changes(str(tmp_path)) is None
    for i in range(514):
        monitor._dispatch(str(tmp_path / f"file-{i}"))
    assert monitor.take_changes(str(tmp_path)) is None


def test_watcher_replays_inflight_event_and_unlocks_slow_scan(tmp_path, monkeypatch):
    """A concurrent event must not wait on a slow post-run filesystem scan."""
    root = tmp_path / "root"
    root.mkdir()
    path = str(root)
    cfg = ScheduleConfig(folders=[FolderJob(path=path, dry_run_verified=True)],
                         schedule_mode="watch", scheduler_enabled=True, watch_quiet_seconds=0.01)
    config = tmp_path / "schedule.json"
    save_config(config, cfg)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(schedule_daemon, "WATCH_EVENT_TICK_SECONDS", 0.01)
    monkeypatch.setattr(schedule_daemon, "_notify_run", lambda *args: None)
    monitor = monitor_without_observer(root)
    monitor.backend_name = lambda: "test"
    monitor.stop = lambda: None
    armed = threading.Event()
    release_scan = threading.Event()
    scan_started = threading.Event()
    stop = threading.Event()
    records = []

    def create(callback):
        monitor._on_dirty = callback
        return monitor

    def set_watches(paths):
        armed.set()

    monitor.set_watched_paths = set_watches
    monkeypatch.setattr(schedule_daemon, "create_event_monitor", create)

    def signature(job):
        if threading.current_thread().name.startswith("watch-organize") and len(records) == 1:
            scan_started.set()
            assert release_scan.wait(2), "test failed to release slow scan"
        return (1.0,)

    monkeypatch.setattr(schedule_daemon, "watch_signature", signature)

    def run(*args, **kwargs):
        records.append(kwargs.get("changed_paths"))
        if len(records) == 2:
            stop.set()
        return {"ran": 1, "failed": 0}

    monkeypatch.setattr(schedule_daemon, "run_enabled_folders", run)
    errors = []

    def daemon():
        try:
            schedule_daemon._run_watch_loop(config, force=False, max_parallel=1,
                                            should_stop=stop.is_set)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=daemon)
    thread.start()
    try:
        assert armed.wait(2)
        monitor._dispatch(str(root / "first.jpg"))
        assert scan_started.wait(2)
        event_delivered = threading.Event()

        def send_event():
            monitor._dispatch(str(root / "during.jpg"))
            event_delivered.set()

        sender = threading.Thread(target=send_event)
        sender.start()
        assert event_delivered.wait(0.5), "event callback blocked by scan lock"
        release_scan.set()
        sender.join(2)
        thread.join(3)
        assert not thread.is_alive()
        assert errors == []
        assert records == [None, {path: {str(root / "during.jpg")}}]
    finally:
        stop.set()
        release_scan.set()
        thread.join(3)


def test_incremental_root_alias(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    (root / "photo.jpg").write_bytes(b"image")
    result = organizer(alias, changed_paths=[alias / "photo.jpg"]).run()
    assert result["files_moved"] == 1
    assert (root / "Images/photo.jpg").exists()


def test_incremental_respects_ancestor_age(tmp_path):
    parent = tmp_path / "new"
    child = parent / "old"
    child.mkdir(parents=True)
    (child / "a.jpg").write_bytes(b"image")
    old = time.time() - 3600
    os.utime(child, (old, old))
    result = organizer(tmp_path, changed_paths=[child / "a.jpg"], empty_dir_min_age=60).run()
    assert result["files_moved"] == 1
    assert parent.exists() and child.exists()
    assert result["empty_folder_collection"]["folders_moved"] == 0


def test_dry_run_does_not_persist_hash_cache(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"same")
    (root / "b.jpg").write_bytes(b"same")
    organizer(root, detect_duplicates=True, dry_run=True).run()
    assert not state.exists()


def test_scheduler_passes_incremental_paths_and_records_metrics(tmp_path, monkeypatch):
    from org_logging import read_history
    from schedule_config import run_enabled_folders

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "root"
    root.mkdir()
    source = root / "changed.jpg"
    source.write_bytes(b"changed")
    untouched = root / "untouched.jpg"
    untouched.write_bytes(b"untouched")
    job = FolderJob(path=str(root), dry_run_verified=True)
    cfg = ScheduleConfig(folders=[job])
    config = tmp_path / "schedule.json"
    save_config(config, cfg)
    result = run_enabled_folders(cfg, config, changed_paths={job.path: {str(source)}})
    assert result["ran"] == 1 and result["failed"] == 0
    assert untouched.exists()
    assert (root / "Images/changed.jpg").exists()
    history = read_history(1)
    assert history[0]["execution"] == "incremental"
    assert "organize" in history[0]["phase_seconds"]
