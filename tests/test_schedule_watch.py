#!/usr/bin/env python3
"""Tests for schedule_watch (native FS event monitor + polling fallback)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import schedule_watch
from schedule_watch import (
    create_event_monitor,
    events_available,
    is_noise_path,
    resolve_watch_root,
)


def test_is_noise_path_filters_metadata_and_organizer_dir():
    assert is_noise_path("/watched/.DS_Store")
    assert is_noise_path("/watched/sub/.DS_Store")
    assert is_noise_path("/watched/Thumbs.db")
    assert is_noise_path("/watched/.organizer/backup_2026.json")
    assert is_noise_path("/watched/deep/.ORGANIZER/manifest.json")
    assert not is_noise_path("/watched/photo.jpg")
    assert not is_noise_path("/watched/sub/deep/clip.mp4")


def test_resolve_watch_root_maps_nested_paths(tmp_path: Path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "a" / "nested"
    root_a.mkdir()
    root_b.mkdir()
    roots = {str(root_a): "~/a", str(root_b): "~/a/nested"}

    # Direct child of the shallow root.
    assert resolve_watch_root(str(root_a / "f.txt"), roots) == "~/a"
    # Deep path under both roots: deepest root wins.
    assert resolve_watch_root(str(root_b / "x" / "y.txt"), roots) == "~/a/nested"
    # The root itself.
    assert resolve_watch_root(str(root_a), roots) == "~/a"
    # Outside every root.
    assert resolve_watch_root(str(tmp_path / "other" / "z.txt"), roots) is None


def test_create_event_monitor_returns_none_without_watchdog(monkeypatch):
    monkeypatch.setattr(schedule_watch, "_WATCHDOG_AVAILABLE", False)
    assert not events_available()
    assert create_event_monitor(lambda p: None) is None


@pytest.mark.skipif(not events_available(), reason="watchdog not installed")
def test_event_monitor_fires_on_deep_change(tmp_path: Path):
    watched = tmp_path / "watched"
    deep = watched / "one" / "two"
    deep.mkdir(parents=True)

    fired = threading.Event()
    hits: list[str] = []

    def on_dirty(job_path: str) -> None:
        hits.append(job_path)
        fired.set()

    monitor = create_event_monitor(on_dirty)
    assert monitor is not None
    try:
        monitor.set_watched_paths([str(watched)])
        assert monitor.watched_job_paths() == {str(watched)}
        # Give the observer a beat to arm the watch.
        time.sleep(0.2)
        (deep / "new-file.txt").write_text("hello")
        assert fired.wait(5.0), "expected a dirty callback for a deep change"
        assert hits[0] == str(watched)
    finally:
        monitor.stop()


@pytest.mark.skipif(not events_available(), reason="watchdog not installed")
def test_event_monitor_ignores_noise_files(tmp_path: Path):
    watched = tmp_path / "watched"
    watched.mkdir()

    fired = threading.Event()
    monitor = create_event_monitor(lambda p: fired.set())
    assert monitor is not None
    try:
        monitor.set_watched_paths([str(watched)])
        time.sleep(0.2)
        (watched / ".DS_Store").write_bytes(b"\x00")
        organizer = watched / ".organizer"
        organizer.mkdir()
        (organizer / "backup_x.json").write_text("{}")
        assert not fired.wait(1.0), "noise files must not mark the root dirty"
    finally:
        monitor.stop()


@pytest.mark.skipif(not events_available(), reason="watchdog not installed")
def test_event_monitor_reconcile_removes_watches(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    monitor = create_event_monitor(lambda p: None)
    assert monitor is not None
    try:
        monitor.set_watched_paths([str(a), str(b)])
        assert monitor.watched_job_paths() == {str(a), str(b)}
        monitor.set_watched_paths([str(b)])
        assert monitor.watched_job_paths() == {str(b)}
        # Missing directories are skipped without raising.
        monitor.set_watched_paths([str(b), str(tmp_path / "missing")])
        assert str(b) in monitor.watched_job_paths()
    finally:
        monitor.stop()
