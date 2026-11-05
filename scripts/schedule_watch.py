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
import os
import stat
import time
import threading
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_manifest import ORGANIZER_DIR_NAME
from org_paths import normalize_folder_input
from org_exclude import merge_exclude_patterns, path_excluded

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


# Files with these names are owned by a writer until their final rename.
PARTIAL_SUFFIXES = (".part", ".partial", ".crdownload", ".download", ".tmp", ".filepart")
_PROTECTED_DIRS = {".organizer", "for deletion", "duplicates", "needs review"}


def is_partial_download(path: str) -> bool:
    return any(part.casefold().endswith(PARTIAL_SUFFIXES) for part in Path(path).parts)


def watch_snapshot(job, *, recursive: bool = True) -> dict:
    """Metadata inventory without following symlinks or reading file content.

    Unlike directory-only mtimes, file size and nanosecond times notice a writer
    appending to an existing file. Fast polling reads only direct root entries;
    periodic recursive scans handle deeper changes.
    """
    base = normalize_folder_input(job.path).resolve()
    patterns = merge_exclude_patterns(job.exclude, use_defaults=job.exclude_defaults)
    entries = {}
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            st = directory.lstat()
            entries[str(directory)] = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            with os.scandir(directory) as stream:
                for entry in stream:
                    path = Path(entry.path)
                    if is_noise_path(str(path)) or path_excluded(path, base, patterns):
                        continue
                    if not job.include_hidden and entry.name.startswith("."):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if entry.name.casefold() in _PROTECTED_DIRS:
                            continue
                        entries[str(path)] = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                        if recursive and job.recursive:
                            stack.append(path)
                    elif stat.S_ISREG(st.st_mode):
                        entries[str(path)] = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        except OSError:
            continue
    return entries


def _candidate_snapshot(job, paths=None) -> dict:
    if paths is None:
        return {path: value for path, value in watch_snapshot(job).items() if stat.S_ISREG(value[2])}
    values = {}
    for value in paths:
        path = Path(value)
        try:
            st = path.lstat()
            if stat.S_ISREG(st.st_mode):
                values[str(path)] = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        except OSError:
            continue
    return values


def readiness_deferred(job, paths=None, *, stable_seconds: float = 1.0, should_stop=lambda: False) -> set:
    """Observe candidate files across a stability interval before a watch run.

    This runs in the folder worker, never on the event-loop thread. A final
    age/identity check in the organizer protects files arriving during execution.
    """
    if job.empty_dirs_only:
        return set()
    before = _candidate_snapshot(job, paths)
    if not before:
        return set()
    deadline = time.monotonic() + stable_seconds
    while time.monotonic() < deadline and not should_stop():
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    after = _candidate_snapshot(job, paths)
    return {path for path, identity in after.items()
            if before.get(path) != identity or is_partial_download(path)}


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
        # Reading a file (including duplicate hashing) does not change it.
        # Inotify reports these events too; treating them as writes causes
        # unnecessary organizer runs and can keep resetting the quiet timer.
        if event.event_type in {"opened", "closed_no_write"}:
            return
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
        self._changes: Dict[str, Optional[set]] = {}
        self._directory_changes: Dict[str, set] = {}
        self._overflows: set = set()
        self._directory_paths: Dict[str, set] = {}
        self._own_mutations: dict = {}
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
            except (OSError, ValueError):
                continue

        with self._lock:
            for root in list(self._watches):
                if root not in desired:
                    try:
                        self._observer.unschedule(self._watches.pop(root))
                    except Exception:
                        self._watches.pop(root, None)
                    old_job = self._roots.pop(root, None)
                    self._changes.pop(old_job, None)
                    self._directory_changes.pop(old_job, None)
                    self._overflows.discard(old_job)
                    self._directory_paths.pop(old_job, None)
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

    def acknowledge_mutations(self, receipts: dict) -> None:
        """Suppress only events whose current identity equals our own mutation.

        An actual concurrent edit changes the identity and remains queued. Both
        already queued events and delayed OS events pass through the same check.
        Receipts expire so a recreated/deleted path is not suppressed forever.
        """
        now = time.monotonic()
        with self._lock:
            existing = getattr(self, "_own_mutations", {})
            self._own_mutations = {path: record for path, record in existing.items() if record[0] > now}
            for path, identity in receipts.items():
                normalized = os.path.abspath(path)
                self._own_mutations[normalized] = (now + 120.0, identity)

    def prune_mutations(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._own_mutations = {path: record for path, record in getattr(self, "_own_mutations", {}).items()
                                   if record[0] > now}

    def _is_own_mutation(self, path: str) -> bool:
        normalized = os.path.abspath(path)
        with self._lock:
            record = getattr(self, "_own_mutations", {}).get(normalized)
        if record is None or record[0] <= time.monotonic():
            return False
        expected = record[1]
        try:
            info = Path(normalized).lstat()
        except FileNotFoundError:
            return expected is None
        except OSError:
            return False
        if expected is None:
            return False
        actual = {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
                  "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
                  "directory": stat.S_ISDIR(info.st_mode)}
        return actual == expected

    def _dispatch(self, path: str, *, is_directory: bool = False) -> None:
        if self._is_own_mutation(path):
            return
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
            with self._lock:
                changes = self._changes.setdefault(job_path, set())
                # Keep directory paths for cleanup-only jobs, which can inspect
                # affected parents without rescanning a populated tree.
                if not hasattr(self, "_directory_changes"):
                    self._directory_changes = {}
                    self._overflows = set()
                if not hasattr(self, "_directory_paths"):
                    self._directory_paths = {}
                directory_changes = self._directory_changes.setdefault(job_path, set())
                if is_directory:
                    self._directory_paths.setdefault(job_path, set()).add(path)
                    directory_changes.update(changes or ())
                    directory_changes.add(path)
                    self._changes[job_path] = None
                elif changes is not None:
                    changes.add(path)
                elif directory_changes:
                    directory_changes.add(path)
                if len(changes or ()) >= 512 or len(directory_changes) >= 512:
                    self._changes[job_path] = None
                    self._overflows.add(job_path)
                    directory_changes.clear()
            self._on_dirty(job_path)

    def take_changes(self, job_path: str, *, directories: bool = False) -> Optional[set]:
        """Drain user changes; empty set means queued events were all our own."""
        with self._lock:
            pending = job_path in self._changes
            result = self._changes.pop(job_path, None)
            paths = getattr(self, "_directory_changes", {}).pop(job_path, set())
            directory_paths = getattr(self, "_directory_paths", {}).pop(job_path, set())
            overflow = job_path in getattr(self, "_overflows", set())
            if hasattr(self, "_overflows"):
                self._overflows.discard(job_path)
        if overflow or not pending:
            return None
        retained = {path for path in (result or set()) | paths if not self._is_own_mutation(path)}
        retained_directories = directory_paths & retained
        if retained_directories and not directories:
            return None
        return retained

    def stop(self) -> None:
        with self._lock:
            watches = list(self._watches.values())
            self._watches.clear()
            self._roots.clear()
            self._changes.clear()
            self._directory_changes.clear()
            self._overflows.clear()
            self._directory_paths.clear()
            self._own_mutations.clear()
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
