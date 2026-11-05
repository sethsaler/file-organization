"""Regression coverage for desktop state, recovery lookup, and large review queues."""
from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace

from command_center import CommandCenterApp, file_content_identity, review_rule_key, scan_review_queue
from org_safety import ManifestIndex, SafetyItem, approve_review_item
from quick_controls import watch_health_summary
from schedule_config import FolderJob, ScheduleConfig, load_config, save_config
from schedule_panel import SchedulePanel


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self):
        self.options = {}
        self.rows = {}
        self.selected = []

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **_options):
        pass

    def pack_forget(self):
        pass

    def selection(self):
        return self.selected

    def selection_set(self, value):
        self.selected = list(value) if isinstance(value, list) else [value]

    def get_children(self):
        return list(self.rows)

    def delete(self, key):
        self.rows.pop(key, None)
        if key in self.selected:
            self.selected.remove(key)

    def insert(self, _parent, _position, iid, values):
        self.rows[iid] = values


def panel_fixture(path, cfg):
    panel = SimpleNamespace(config_path=path, cfg=cfg, _config_baseline=copy.deepcopy(cfg))
    pairs = {
        'scheduler_var': 'scheduler_enabled', 'schedule_mode_var': 'schedule_mode',
        'daily_time_var': 'daily_time', 'interval_var': 'interval_minutes',
        'max_parallel_var': 'max_parallel', 'notify_on_run_var': 'notify_on_run',
        'watch_poll_var': 'watch_poll_seconds', 'watch_quiet_var': 'watch_quiet_seconds',
    }
    for variable, field in pairs.items():
        setattr(panel, variable, Value(getattr(cfg, field)))
    for name in ('_sync_schedule_timing_to_cfg', '_sync_parallel_to_cfg', '_sync_notify_to_cfg', '_refresh_tree', '_cancel_add_preview'):
        setattr(panel, name, lambda: None)
    panel._apply_global_values = MethodType(SchedulePanel._apply_global_values, panel)
    panel._persist_config = MethodType(SchedulePanel._persist_config, panel)
    return panel


def test_rule_content_identity_invalidates_same_size_same_mtime(tmp_path):
    path = tmp_path / 'rules.json'
    path.write_text('{"a":1}')
    stat = path.stat()
    before = file_content_identity(str(path))
    path.write_text('{"a":2}')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert before != file_content_identity(str(path))


def test_stale_preview_never_authorizes_changed_settings():
    panel = SimpleNamespace(_preview_request_fingerprint=('folder-a',),
                            _preview_fingerprint=lambda: ('folder-b',),
                            preview_subtitle_var=Value())
    panel._invalidate_preview = lambda: setattr(panel, 'invalidated', True)
    CommandCenterApp._handle_preview_results(panel, [{'returncode': 0, 'stdout': '{}'}])
    assert panel.invalidated
    assert 'Preview again' in panel.preview_subtitle_var.get()


def test_closing_gui_preserves_menu_pause(tmp_path):
    path = tmp_path / 'schedule.json'
    cfg = ScheduleConfig(scheduler_enabled=True)
    save_config(path, cfg)
    panel = panel_fixture(path, cfg)
    disk = load_config(path)
    disk.scheduler_enabled = False
    save_config(path, disk)
    SchedulePanel.shutdown(panel)
    assert not load_config(path).scheduler_enabled
    assert not panel.scheduler_var.get()


def test_external_changes_merge_with_unsaved_settings(tmp_path):
    path = tmp_path / 'schedule.json'
    cfg = ScheduleConfig(scheduler_enabled=True, folders=[FolderJob(path='/a')])
    save_config(path, cfg)
    panel = panel_fixture(path, cfg)
    panel.cfg.interval_minutes = 75  # locally edited but not saved
    disk = load_config(path)
    disk.scheduler_enabled = False
    disk.folders.append(FolderJob(path='/b'))
    save_config(path, disk)
    SchedulePanel._reload_config_if_changed(panel)
    assert not panel.scheduler_var.get()
    assert panel.cfg.interval_minutes == 75
    assert {job.path for job in panel.cfg.folders} == {'/a', '/b'}


def test_manifest_index_loads_once_per_refresh(tmp_path, monkeypatch):
    import org_safety
    directory = tmp_path / '.organizer'
    directory.mkdir()
    paths = []
    for i in range(20):
        path = directory / f'backup_{i:03}.json'
        path.write_text(json.dumps({'base_path': str(tmp_path), 'file_moves': [
            {'from': f'source-{i}.jpg', 'to': f'Needs Review/group-{i}/photo.jpg'}]}))
        paths.append(path)
    calls = []
    read = org_safety.read_manifest_data
    monkeypatch.setattr(org_safety, 'read_manifest_data', lambda path: (calls.append(path), read(path))[1])
    index = ManifestIndex([tmp_path])
    for _ in range(10):
        for i in range(20):
            item = SafetyItem(tmp_path, 'Needs Review', tmp_path / f'Needs Review/group-{i}', 1, 1, 0)
            assert org_safety.manifest_for_item(item, index) is not None
    assert len(calls) == 20


def test_review_approval_uses_recoverable_collision_safe_move(tmp_path):
    review = tmp_path / 'Needs Review'
    review.mkdir()
    destination = tmp_path / 'Other'
    destination.mkdir()
    (review / 'photo.jpg').write_bytes(b'new')
    (destination / 'photo.jpg').write_bytes(b'existing')
    target, manifest = approve_review_item(tmp_path, review / 'photo.jpg', 'Other')
    assert target != destination / 'photo.jpg'
    assert (destination / 'photo.jpg').read_bytes() == b'existing'
    assert target.read_bytes() == b'new'
    assert Path(manifest).is_file()
    index = ManifestIndex([tmp_path])
    assert index.original_source(target) == review / 'photo.jpg'


def test_review_queue_keeps_more_than_1000_and_paginates(tmp_path):
    review = tmp_path / 'Needs Review'
    review.mkdir()
    for i in range(1005):
        (review / f'photo-{i}.jpg').write_bytes(b'x')
    rows = scan_review_queue(tmp_path, threading.Event())
    assert len(rows) == 1005
    app = SimpleNamespace(_review_rows=rows, _review_page=10, review_search_var=Value(''),
                          review_type_var=Value('All files'), review_root_var=Value(str(tmp_path)),
                          review_status_var=Value(), review_tree=Widget(),
                          review_previous=Widget(), review_next=Widget(), _review_items=[])
    app._capture_review_selection = MethodType(CommandCenterApp._capture_review_selection, app)
    CommandCenterApp._render_review_page(app)
    assert len(app._review_items) == 5
    assert '1001–1005' in app.review_status_var.get()
    app.review_search_var.set('photo-1004')
    CommandCenterApp._render_review_page(app)
    assert len(app._review_items) == 1
    assert app._review_page == 0


def test_review_group_key_matches_rule_semantics():
    assert review_rule_key(Path('one/a.JPG'), 'Extension') == review_rule_key(Path('two/b.jpg'), 'Extension')
    assert review_rule_key(Path('creator/a.jpg'), 'Parent folder') == 'creator'


def test_mode_and_stale_health():
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    cfg = ScheduleConfig(scheduler_enabled=True, schedule_mode='daily', daily_time='08:30')
    assert '08:30' in watch_health_summary(cfg, {}, True, now=now)['health_message']
    cfg.schedule_mode = 'watch'
    stale = {'active': True, 'updated_at': (now - timedelta(minutes=5)).isoformat()}
    assert watch_health_summary(cfg, stale, True, now=now)['health_state'] == 'stale'
    fresh = {'active': True, 'updated_at': now.isoformat()}
    assert watch_health_summary(cfg, fresh, True, now=now)['health_state'] == 'watching'
    assert watch_health_summary(cfg, fresh, False, now=now)['health_state'] == 'unavailable'
    cfg.scheduler_enabled = False
    assert watch_health_summary(cfg, fresh, False, now=now)['health_state'] == 'paused'


def test_service_check_is_cached_and_runs_off_ui_thread(monkeypatch):
    import schedule_panel
    calls = []
    monkeypatch.setattr(schedule_panel, 'is_service_running', lambda: calls.append(threading.get_ident()) or True)
    callbacks = queue.Queue()
    panel = SimpleNamespace(status_visible=True, _service_check_running=False, _service_checked_at=0,
                            _queue_ui=callbacks.put, _update_next_run_label=lambda: None)
    main_thread = threading.get_ident()
    SchedulePanel._request_service_status(panel)
    callbacks.get(timeout=2)()
    SchedulePanel._request_service_status(panel)
    assert len(calls) == 1 and calls[0] != main_thread
    assert panel._service_snapshot['running'] is True


def test_add_folder_preview_can_be_cancelled_without_adding(tmp_path, monkeypatch):
    import schedule_panel
    import sys
    monkeypatch.setattr(schedule_panel, 'build_organize_cmd', lambda *_args, **_kwargs: [sys.executable, '-c', 'import time; time.sleep(20)'])
    callbacks = queue.Queue()
    panel = SimpleNamespace(cfg=ScheduleConfig(), _add_preview_cancel=None,
                            add_preview_status=Value(), cancel_add_preview_button=Widget(),
                            _queue_ui=callbacks.put)
    panel._cancel_add_preview = MethodType(SchedulePanel._cancel_add_preview, panel)
    started = time.monotonic()
    assert SchedulePanel.add_folder_path(panel, str(tmp_path))
    assert time.monotonic() - started < 1
    SchedulePanel._cancel_add_preview(panel)
    callbacks.get(timeout=3)()
    assert panel.cfg.folders == []


def test_busy_preview_request_cannot_replace_running_request(monkeypatch):
    import command_center
    panel = SimpleNamespace(_worker=SimpleNamespace(is_alive=lambda: True),
                            _preview_request_fingerprint=('folder-a',))
    monkeypatch.setattr(command_center.messagebox, 'showwarning', lambda *_args: None)
    CommandCenterApp._start_jobs(panel, [('b', ['unused'])], 'preview')
    assert panel._preview_request_fingerprint == ('folder-a',)


def test_cancelled_background_result_is_discarded():
    calls = []
    cancelled = threading.Event()
    cancelled.set()
    incoming = queue.Queue()
    incoming.put(('safety', cancelled, lambda *_args: calls.append('applied'), [], None))
    app = SimpleNamespace(_closed=False, _background_queue=incoming,
                          _background_tasks={}, root=SimpleNamespace(after=lambda *_args: None))
    app._poll_background = MethodType(CommandCenterApp._poll_background, app)
    app._poll_background()
    assert calls == []


def test_manifest_index_discovers_unfinished_journal(tmp_path):
    from org_manifest import RecoveryJournal
    source = tmp_path / 'photo.jpg'
    source.write_text('original')
    target = tmp_path / 'Needs Review' / 'photo.jpg'
    target.parent.mkdir()
    journal = RecoveryJournal(tmp_path)
    journal.move(source, target)
    index = ManifestIndex([tmp_path])
    assert index.manifest_for(target) == journal.manifest_path
    assert index.original_source(target) == source
    journal.finish()


def test_review_selection_survives_page_changes(tmp_path):
    rows = [(tmp_path / "Needs Review" / f"photo-{i:03}.jpg", 1, 0) for i in range(205)]
    app = SimpleNamespace(_review_rows=rows, _review_page=0, _review_items=[],
                          _review_selected_paths=set(), review_search_var=Value(''),
                          review_type_var=Value('All files'), review_root_var=Value(str(tmp_path)),
                          review_status_var=Value(), review_tree=Widget(),
                          review_previous=Widget(), review_next=Widget())
    app._capture_review_selection = MethodType(CommandCenterApp._capture_review_selection, app)
    CommandCenterApp._render_review_page(app)
    app.review_tree.selection_set(['review-0', 'review-1'])
    app._review_page = 1
    CommandCenterApp._render_review_page(app)
    assert app._review_selected_paths == {rows[0][0], rows[1][0]}
    app._review_page = 0
    CommandCenterApp._render_review_page(app)
    assert set(app.review_tree.selection()) == {'review-0', 'review-1'}


def test_service_reconciliation_applies_new_pause_during_start(tmp_path, monkeypatch):
    import quick_controls
    path = tmp_path / 'schedule.json'
    save_config(path, ScheduleConfig(scheduler_enabled=True))
    calls = []
    def sync(enabled):
        calls.append(enabled)
        if enabled:
            cfg = load_config(path)
            cfg.scheduler_enabled = False
            save_config(path, cfg)
        return True, 'applied'
    monkeypatch.setattr(quick_controls, 'sync_service_enabled', sync)
    assert quick_controls.reconcile_background_service(path)[0]
    assert calls == [True, False]
    assert not load_config(path).scheduler_enabled


def test_service_reconciliation_serializes_workers(tmp_path, monkeypatch):
    import quick_controls
    path = tmp_path / 'schedule.json'
    save_config(path, ScheduleConfig(scheduler_enabled=True))
    active = []
    overlapping = []
    def sync(enabled):
        if active:
            overlapping.append(True)
        active.append(enabled)
        time.sleep(0.02)
        active.pop()
        return True, 'applied'
    monkeypatch.setattr(quick_controls, 'sync_service_enabled', sync)
    workers = [threading.Thread(target=quick_controls.reconcile_background_service, args=(path,)) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()
    assert overlapping == []


def test_old_service_failure_callback_cannot_override_new_intent(tmp_path, monkeypatch):
    import schedule_panel
    warnings = []
    callbacks = queue.Queue()
    monkeypatch.setattr(schedule_panel, 'reconcile_background_service', lambda *_args, **_kwargs: (False, 'failed'))
    monkeypatch.setattr(schedule_panel.messagebox, 'showwarning', lambda *_args: warnings.append(True))
    panel = SimpleNamespace(_service_generation=0, config_path=tmp_path/'schedule.json',
                            _log_queue=queue.Queue(), _queue_ui=callbacks.put,
                            _request_service_status=lambda **_kwargs: None,
                            _update_next_run_label=lambda: None, scheduler_var=Value(True))
    SchedulePanel._sync_background_service(panel)
    old_finish = callbacks.get(timeout=2)
    SchedulePanel._sync_background_service(panel)
    new_finish = callbacks.get(timeout=2)
    old_finish()
    assert warnings == []
    new_finish()
    assert warnings == [True]
    assert panel.scheduler_var.get() is True
