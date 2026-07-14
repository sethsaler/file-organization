#!/usr/bin/env python3
"""Native filesystem-event watching for watch mode (optional `watchdog` backend).

When the `watchdog` package is installed this module provides near-instant,
recursive change detection for watched folders via the platform's native API
(FSEvents on macOS, inotify on Linux, ReadDirectoryChangesW on Windows).

When `watchdog` is not installed, `create_event_monitor()` returns None and the
daemon falls back to the existing mtime-polling loop unchanged.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_manifest import ORGANIZER_DIR_NAME
from org_paths import normalize_folder_input

try:  # pragma: no cover - import guard exercised via events_available()
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    _WATCHDOG_AVAILABLE = False


# File names whose changes should never trigger a reorganization run.
IGNORED_BASENAMES = frozenset({".DS_Store", ".localized", "Thumbs.db", "desktop.ini"})


def events_available() -> bool:
    """True when the watchdog package can supply native FS events."""
    return _WATCHDOG_AVAILABLE


def is_noise_path(path: str) -> bool:
    """Paths whose changes must not mark a watched root dirty.

    Filters macOS/Windows folder metadata and everything under the organizer's
    own ``.organizer`` backup directory (manifests written during a run).
    """
    p = Path(path)
    if p.name in IGNORED_BASENAMES:
        return True
    organizer = ORGANIZER_DIR_NAME.casefold()
    return any(part.casefold() == organizer for part in p.parts)


def resolve_watch_root(path: str, roots: Dict[str, str]) -> Optional[str]:
    """Map an event path to the configured job path of the watched root
    containing it.

    ``roots`` maps normalized absolute root paths -> original job path strings
    (as stored in schedule.json). Deepest root wins when watches nest.
    """
    try:
        candidate = Path(path).resolve()
    except OSError:
        candidate = Path(path)
    best: Optional[str] = None
    best_len = -1
    for root, job_path in roots.items():
        root_path = Path(root)
        if candidate == root_path or root_path in candidate.parents:
            if len(root) > best_len:
                best = job_path
                best_len = len(root)
    return best


class _DirtyHandler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, monitor: "WatchEventMonitor") -> None:
        self._monitor = monitor

    def on_any_event(self, event) -> None:  # noqa: ANN001 - watchdog event type
        # Directory-modified events fire for every contained file change; the
        # file-level events already cover those, and pure directory mtime churn
        # (e.g. Finder metadata) should not trigger runs on its own.
        is_directory = bool(getattr(event, "is_directory", False))
        if is_directory and event.event_type == "modified":
            return
        paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
        for raw in paths:
            if not raw:
                continue
            path = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if is_noise_path(path):
                continue
            self._monitor._dispatch(path, is_directory=is_directory)
            return


class WatchEventMonitor:
    """Owns a watchdog Observer with one recursive watch per enabled folder.

    Calls ``on_dirty(job_path)`` (from an observer thread) whenever anything
    changes at any depth beneath a watched root. Callers reconcile the watched
    set via :meth:`set_watched_paths` (safe to call repeatedly, e.g. on config
    reload).
    """

    def __init__(self, on_dirty: Callable[[str], None]) -> None:
        if not _WATCHDOG_AVAILABLE:
            raise RuntimeError("watchdog is not installed")
        self._on_dirty = on_dirty
        self._lock = threading.Lock()
        self._roots: Dict[str, str] = {}  # normalized root -> job path
        self._watches: Dict[str, object] = {}  # normalized root -> ObservedWatch
        self._handler = _DirtyHandler(self)
        self._observer = Observer()
        self._observer.daemon = True
        self._observer.start()

    def backend_name(self) -> str:
        name = type(self._observer).__name__.casefold()
        if "fsevents" in name or "kqueue" in name:
            return "fsevents" if "fsevents" in name else "kqueue"
        if "inotify" in name:
            return "inotify"
        if "polling" in name:
            return "watchdog-polling"
        return name or "watchdog"

    def set_watched_paths(self, job_paths: Iterable[str]) -> None:
        """Reconcile observer watches with the enabled folder set.

        Roots that fail to schedule (e.g. missing directory) are skipped and
        retried on the next call.
        """
        desired: Dict[str, str] = {}
        for job_path in job_paths:
            try:
                # Resolve symlinks so roots compare equal to event paths, which
                # the OS reports fully resolved (e.g. /var -> /private/var on
                # macOS).
                desired[str(normalize_folder_input(job_path).resolve())] = job_path
            except Exception:
                continue

        with self._lock:
            for root in list(self._watches):
                if root not in desired:
                    try:
                        self._observer.unschedule(self._watches.pop(root))
                    except Exception:
                        self._watches.pop(root, None)
                    self._roots.pop(root, None)
            for root, job_path in desired.items():
                self._roots[root] = job_path
                if root in self._watches:
                    continue
                try:
                    self._watches[root] = self._observer.schedule(
                        self._handler, root, recursive=True
                    )
                except Exception:
                    # Missing/unreadable directory; retried on next reconcile.
                    self._roots.pop(root, None)
                    continue

    def watched_job_paths(self) -> set:
        with self._lock:
            return set(self._roots.values())

    def _dispatch(self, path: str, *, is_directory: bool = False) -> None:
        with self._lock:
            roots = dict(self._roots)
        if is_directory:
            # Directory events for a watched root itself are startup/attribute
            # churn (FSEvents can replay the root's own creation right after a
            # watch is scheduled); real content changes fire their own events.
            try:
                resolved = str(Path(path).resolve())
            except OSError:
                resolved = path
            if resolved in roots or path in roots:
                return
        job_path = resolve_watch_root(path, roots)
        if job_path is not None:
            self._on_dirty(job_path)

    def stop(self) -> None:
        with self._lock:
            watches = list(self._watches.values())
            self._watches.clear()
            self._roots.clear()
        for watch in watches:
            try:
                self._observer.unschedule(watch)
            except Exception:
                pass
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:
            pass


def create_event_monitor(on_dirty: Callable[[str], None]) -> Optional[WatchEventMonitor]:
    """Return a running WatchEventMonitor, or None when watchdog is unavailable
    (callers fall back to mtime polling)."""
    if not _WATCHDOG_AVAILABLE:
        return None
    try:
        return WatchEventMonitor(on_dirty)
    except Exception:
        return None
