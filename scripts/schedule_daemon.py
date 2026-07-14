#!/usr/bin/env python3
"""Background scheduler: reload schedule.json on each cycle and run enabled folders in parallel.

Typical use:
  - systemd: run `schedule_daemon.py --foreground` as a long-lived service; enable `scheduler_enabled`
    in the JSON (via schedule_gui.py).
  - cron: call `schedule_daemon.py --once` every N minutes; each invocation runs one parallel batch
    if `scheduler_enabled` is true.

Config path defaults to the same file as the GUI: ~/.config/file-organization/schedule.json
(XDG_CONFIG_HOME respected). See schedule_config.py for `max_parallel` (0 = all enabled at once, max 32).
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional, Set

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_logging import default_log_path, default_state_dir
from schedule_config import (
    SCHEDULE_MODE_WATCH,
    FolderJob,
    default_config_path,
    load_config,
    normalize_schedule_mode,
    run_enabled_folders,
    wait_seconds_after_run,
    watch_signature,
    watch_signature_fast,
)
from schedule_watch import create_event_monitor

WATCH_CONFIG_RELOAD_SECONDS = 30.0

# Polling fallback (no watchdog installed): how often the watch loop does a full
# recursive mtime scan to catch changes that occurred deeper than one level under
# the watched root. The fast signature (root + immediate subdirs) runs every poll.
WATCH_FULL_SCAN_SECONDS = 5.0

# Event mode (watchdog installed): loop tick while waiting for events to settle.
# No filesystem I/O happens on this tick — it only ages the dirty/quiet state.
WATCH_EVENT_TICK_SECONDS = 0.1

# Event mode safety net: native events can theoretically be dropped, so a full
# recursive mtime scan still runs occasionally to catch anything missed.
WATCH_SAFETY_SCAN_SECONDS = 60.0


def _watch_state_path() -> Path:
    return default_state_dir() / "watch-signatures.json"


def _load_watch_signatures() -> Dict[str, list]:
    try:
        data = json.loads(_watch_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_watch_signatures(cfg) -> None:
    """Persist post-run signatures so the next --once fire can tell which folder changed."""
    sigs = {
        job.path: list(watch_signature(job))
        for job in cfg.folders
        if job.enabled
    }
    path = _watch_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sigs), encoding="utf-8")
    except OSError:
        pass


def _changed_watch_paths(cfg) -> Optional[Set[str]]:
    """Enabled folders whose watch signature differs from the persisted state.

    Returns None to run everything: either there is no prior state, or nothing
    changed — the latter is the hourly StartInterval backstop firing, which must
    still sweep all folders to catch deep changes the signature cannot see.
    """
    stored = _load_watch_signatures()
    if not stored:
        return None
    changed: Set[str] = set()
    for job in cfg.folders:
        if not job.enabled:
            continue
        prev = stored.get(job.path)
        if prev is None:
            changed.add(job.path)
            continue
        try:
            prev_sig = tuple(float(x) for x in prev)
        except (TypeError, ValueError):
            changed.add(job.path)
            continue
        if watch_signature(job) != prev_sig:
            changed.add(job.path)
    return changed or None


def _notify_run(cfg, summary: Optional[dict], label: str) -> None:
    """Post a macOS notification after a background sweep actually ran folders."""
    if sys.platform != "darwin" or not getattr(cfg, "notify_on_run", True):
        return
    if not summary or summary.get("ran", 0) <= 0:
        return
    ran = summary.get("ran", 0)
    failed = summary.get("failed", 0)
    msg = f"Organized {ran} folder(s)"
    if failed:
        msg += f", {failed} failed"
    script = (
        f'display notification "{msg}" '
        f'with title "File Organization" subtitle "{label} run"'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run scheduled folder organization in the background.")
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to schedule.json (default: {default_config_path()})",
    )
    p.add_argument(
        "--foreground",
        action="store_true",
        help="Loop forever (use with systemd or tmux); respects schedule_mode, interval/daily_time, and scheduler_enabled in JSON",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Single batch then exit (for cron); honors scheduler_enabled",
    )
    p.add_argument(
        "--interval-override",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Ignore interval_minutes in JSON; use this many minutes between runs (--foreground only)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Run enabled folders even when scheduler_enabled is false (still updates last_run)",
    )
    p.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        metavar="N",
        help="Override max_parallel from JSON for this process (0=all at once up to 32)",
    )
    p.add_argument(
        "--no-run-on-start",
        action="store_true",
        help="With --foreground, sleep first so the first run waits until the next interval or daily time",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config_path = args.config.expanduser() if args.config else default_config_path()

    if args.foreground and args.once:
        print(json.dumps({"error": "Use only one of --foreground or --once"}, indent=2))
        sys.exit(2)

    if args.once:
        cfg = load_config(config_path)
        if not args.force and not cfg.scheduler_enabled:
            print(json.dumps({"skipped": True, "reason": "scheduler_enabled is false", "config": str(config_path)}, indent=2))
            return
        watch_mode = normalize_schedule_mode(cfg.schedule_mode) == SCHEDULE_MODE_WATCH
        only_paths = _changed_watch_paths(cfg) if watch_mode else None
        summary = run_enabled_folders(
            cfg,
            config_path,
            max_parallel=args.max_parallel,
            log=lambda s: print(s, end=""),
            label="once",
            file_log_path=default_log_path(),
            only_paths=only_paths,
        )
        if watch_mode:
            # Baseline post-run signatures so the run's own moves (and unchanged
            # folders) do not count as changes on the next fire.
            _save_watch_signatures(cfg)
        _notify_run(cfg, summary, "scheduled")
        out: dict = {"ok": True, "config": str(config_path)}
        if only_paths is not None:
            out["only_changed"] = sorted(only_paths)
        print(json.dumps(out, indent=2))
        return

    if not args.foreground:
        print(json.dumps({"error": "Specify --foreground (daemon loop) or --once (cron)"}, indent=2))
        sys.exit(2)

    stop = False

    def handle_sig(_sig: int, _frame: Optional[object]) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    first = True
    while not stop:
        cfg = load_config(config_path)

        if args.interval_override is None and normalize_schedule_mode(cfg.schedule_mode) == SCHEDULE_MODE_WATCH:
            _run_watch_loop(config_path, force=args.force, max_parallel=args.max_parallel, should_stop=lambda: stop)
            return
        if args.interval_override is not None:
            wait_sec = max(60.0, float(max(1, min(10080, int(args.interval_override))) * 60))
        else:
            wait_sec = wait_seconds_after_run(cfg)

        if first and args.no_run_on_start:
            first = False
            remaining = wait_sec
            while remaining > 0:
                if stop:
                    return
                step = min(remaining, 1.0)
                time.sleep(step)
                remaining -= step
            continue

        if args.force or cfg.scheduler_enabled:
            summary = run_enabled_folders(
                cfg,
                config_path,
                max_parallel=args.max_parallel,
                log=lambda s: print(s, end=""),
                label="scheduled",
                file_log_path=default_log_path(),
            )
            _notify_run(cfg, summary, "scheduled")

        first = False
        cfg = load_config(config_path)
        if args.interval_override is not None:
            wait_sec = max(60.0, float(max(1, min(10080, int(args.interval_override))) * 60))
        else:
            wait_sec = wait_seconds_after_run(cfg)
        remaining = wait_sec
        while remaining > 0:
            if stop:
                return
            step = min(remaining, 1.0)
            time.sleep(step)
            remaining -= step


def _run_watch_loop(
    config_path: Path,
    *,
    force: bool,
    max_parallel: Optional[int],
    should_stop,
) -> None:
    """React to folder changes in near real time.

    Preferred backend: native filesystem events via the optional `watchdog`
    package (FSEvents/inotify) — changes at any depth beneath a watched root are
    detected instantly, with a relaxed full-scan safety net every
    WATCH_SAFETY_SCAN_SECONDS in case an event is ever dropped.

    Fallback (watchdog not installed): a lightweight fast signature (watched
    root + immediate subdirs) every cfg.watch_poll_seconds, plus a full
    recursive scan every WATCH_FULL_SCAN_SECONDS to catch deep changes.

    In both modes, each folder gets its own background worker once it has
    stayed quiet for cfg.watch_quiet_seconds, so a long-running folder cannot
    stop the watcher from noticing and organizing other folders.
    """
    cfg = load_config(config_path)
    state_lock = threading.Lock()
    signatures_fast: dict = {}
    signatures_full: dict = {}
    dirty_since: dict = {}
    in_flight: set = set()
    last_reload = 0.0
    last_full_scan = 0.0
    file_log_path = default_log_path()

    mp = max_parallel if max_parallel is not None else cfg.max_parallel
    max_workers = max(1, min(mp, 32)) if mp and mp > 0 else 32
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="watch-organize")

    def _mark_dirty(path: str) -> None:
        """Native event callback (observer thread): mark a watched root dirty."""
        with state_lock:
            if path in in_flight:
                # The organizer's own moves fire events for the root being
                # organized; ignore them (baselines are reset after the run).
                return
            dirty_since[path] = time.monotonic()

    monitor = create_event_monitor(_mark_dirty)
    use_events = monitor is not None
    if use_events:
        monitor.set_watched_paths(job.path for job in cfg.folders if job.enabled)

    print(json.dumps({
        "watch": True,
        "backend": monitor.backend_name() if use_events else "polling",
        "poll_seconds": WATCH_EVENT_TICK_SECONDS if use_events else cfg.watch_poll_seconds,
        "quiet_seconds": cfg.watch_quiet_seconds,
        "full_scan_seconds": WATCH_SAFETY_SCAN_SECONDS if use_events else WATCH_FULL_SCAN_SECONDS,
        "max_workers": max_workers,
    }, indent=2))

    def _update_both_signatures(path: str, job: Optional[FolderJob] = None) -> None:
        target = job if job is not None else FolderJob(path=path)
        if not use_events:
            signatures_fast[path] = watch_signature_fast(target)
        signatures_full[path] = watch_signature(target)

    def _organize_path(path: str) -> None:
        sub_cfg: Optional[object] = None
        try:
            sub_cfg = load_config(config_path)
            summary = run_enabled_folders(
                sub_cfg,
                config_path,
                max_parallel=1,
                log=lambda s: print(s, end=""),
                label="watch",
                file_log_path=file_log_path,
                only_paths={path},
            )
            if summary:
                _notify_run(sub_cfg, summary, "watch")
        finally:
            with state_lock:
                in_flight.discard(path)
                if sub_cfg is not None:
                    # Update baselines so the organizer's own moves do not retrigger.
                    for job in sub_cfg.folders:
                        if job.path == path:
                            _update_both_signatures(path, job)
                            break
                    else:
                        _update_both_signatures(path)
                dirty_since.pop(path, None)

    full_scan_interval = WATCH_SAFETY_SCAN_SECONDS if use_events else WATCH_FULL_SCAN_SECONDS

    try:
        while not should_stop():
            now = time.monotonic()
            if now - last_reload >= WATCH_CONFIG_RELOAD_SECONDS:
                cfg = load_config(config_path)
                last_reload = now
                if normalize_schedule_mode(cfg.schedule_mode) != SCHEDULE_MODE_WATCH:
                    print(json.dumps({"watch": False, "reason": "schedule_mode changed"}, indent=2))
                    return
                if use_events:
                    monitor.set_watched_paths(
                        job.path for job in cfg.folders if job.enabled
                    )

            do_full_scan = now - last_full_scan >= full_scan_interval
            if do_full_scan:
                last_full_scan = now

            if force or cfg.scheduler_enabled:
                due_paths = set()
                enabled = [job for job in cfg.folders if job.enabled]
                new_fast_sigs = (
                    {job.path: watch_signature_fast(job) for job in enabled}
                    if not use_events else {}
                )
                new_full_sigs = (
                    {job.path: watch_signature(job) for job in enabled}
                    if do_full_scan else {}
                )

                with state_lock:
                    for job in enabled:
                        path = job.path
                        if path in in_flight:
                            continue
                        sig_changed = False

                        if not use_events:
                            fast_prev = signatures_fast.get(path)
                            fast_sig = new_fast_sigs[path]
                            signatures_fast[path] = fast_sig
                            if fast_prev is not None and fast_sig != fast_prev:
                                sig_changed = True

                        if do_full_scan:
                            full_prev = signatures_full.get(path)
                            full_sig = new_full_sigs[path]
                            signatures_full[path] = full_sig
                            if full_prev is not None and full_sig != full_prev:
                                sig_changed = True

                        if sig_changed:
                            dirty_since[path] = now
                            continue

                        started = dirty_since.get(path)
                        if started is not None and now - started >= cfg.watch_quiet_seconds:
                            due_paths.add(path)
                            del dirty_since[path]
                            in_flight.add(path)

                for path in due_paths:
                    executor.submit(_organize_path, path)

            poll_seconds = WATCH_EVENT_TICK_SECONDS if use_events else cfg.watch_poll_seconds
            remaining = poll_seconds
            while remaining > 0:
                if should_stop():
                    return
                step = min(remaining, 1.0)
                time.sleep(step)
                remaining -= step
    finally:
        if monitor is not None:
            monitor.stop()
        executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
