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
from org_watch_status import WatchStatus
from schedule_config import (
    SCHEDULE_MODE_WATCH,
    FolderJob,
    auto_disabled_jobs,
    default_config_path,
    is_auto_disabled,
    load_config,
    normalize_schedule_mode,
    run_enabled_folders,
    wait_seconds_after_run,
    build_organize_cmd,
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

# Extra wait past an empty folder's grace window before re-running, so the
# re-run does not land a hair early and defer the folder again.
EMPTY_RECHECK_SLACK_SECONDS = 0.5


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
    # Empty-folder cleanup runs can fire every few seconds; only announce real organizing.
    ran = summary.get("ran", 0) - summary.get("cleanup_only", 0)
    failed = summary.get("failed", 0)
    disabled = summary.get("disabled", 0)
    if ran <= 0 and not failed and not disabled:
        return
    msg = f"Organized {ran} folder(s)"
    if failed:
        msg += f", {failed} failed"
    if disabled:
        msg += f"; {disabled} switched off after repeated failures"
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
    reconciled: dict = {}
    maintenance_due: set = set()
    full_required: set = set()
    incremental_batches: dict = {}
    primed_cleanup: set = set()
    last_reload = 0.0
    last_full_scan = 0.0
    last_heartbeat = 0.0
    file_log_path = default_log_path()
    status: Optional[WatchStatus] = None

    mp = max_parallel if max_parallel is not None else cfg.max_parallel
    max_workers = max(1, min(mp, 32)) if mp and mp > 0 else 32
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="watch-organize")

    def _mark_dirty(path: str) -> None:
        """Native event callback (observer thread): mark a watched root dirty."""
        with state_lock:
            # Retain events during a run, including user writes concurrent with
            # organizer moves. Replaying an already-routed file is a cheap no-op.
            dirty_since[path] = time.monotonic()
            if status is not None:
                status.update_folder(path, "dirty", quiet_seconds=cfg.watch_quiet_seconds)

    monitor = create_event_monitor(_mark_dirty)
    use_events = monitor is not None
    if use_events:
        monitor.set_watched_paths(job.path for job in cfg.folders if job.enabled)

    backend = monitor.backend_name() if use_events else "polling"
    status = WatchStatus(
        backend=backend,
        poll_seconds=WATCH_EVENT_TICK_SECONDS if use_events else cfg.watch_poll_seconds,
        quiet_seconds=cfg.watch_quiet_seconds,
        full_scan_seconds=WATCH_SAFETY_SCAN_SECONDS if use_events else WATCH_FULL_SCAN_SECONDS,
        max_workers=max_workers,
        folders=(job.path for job in cfg.folders if job.enabled),
        disabled={job.path: job.last_error for job in auto_disabled_jobs(cfg)},
    )

    print(json.dumps({
        "watch": True,
        "backend": backend,
        "poll_seconds": WATCH_EVENT_TICK_SECONDS if use_events else cfg.watch_poll_seconds,
        "quiet_seconds": cfg.watch_quiet_seconds,
        "full_scan_seconds": WATCH_SAFETY_SCAN_SECONDS if use_events else WATCH_FULL_SCAN_SECONDS,
        "max_workers": max_workers,
    }, indent=2))

    def _organize_path(path: str) -> None:
        sub_cfg: Optional[object] = None
        summary: Optional[dict] = None
        run_error: Optional[str] = None
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
                **({"changed_paths": incremental_batches[path]} if path in incremental_batches else {}),
            )
            if summary:
                _notify_run(sub_cfg, summary, "watch")
        except Exception as exc:
            run_error = str(exc)
        finally:
            # Scan without holding the event-state lock. Other roots and event
            # callbacks remain responsive even on slow network/cloud folders.
            job = next((j for j in sub_cfg.folders if j.path == path), None) if sub_cfg else None
            target = job if job is not None else FolderJob(path=path)
            try:
                full_signature = watch_signature(target)
                fast_signature = watch_signature_fast(target) if not use_events else None
            except OSError:
                full_signature = None
                fast_signature = None
            with state_lock:
                if full_signature is not None:
                    signatures_full[path] = full_signature
                if fast_signature is not None:
                    signatures_fast[path] = fast_signature
                batch = incremental_batches.pop(path, None)
                succeeded = summary and summary.get("ran", 0) and not summary.get("failed", 0) and not run_error
                if succeeded and job is not None:
                    reconciled[path] = build_organize_cmd(job)
                    if batch:
                        maintenance_due.add(path)
                    else:
                        maintenance_due.discard(path)
                        full_required.discard(path)
                elif not succeeded:
                    reconciled.pop(path, None)
                in_flight.discard(path)
                recheck = summary.get("recheck_seconds") if summary else None
                if recheck is not None:
                    # An empty folder is still inside its grace window and no further
                    # event may arrive for it: come back once it becomes removable.
                    due_at = time.monotonic() + recheck + EMPTY_RECHECK_SLACK_SECONDS - cfg.watch_quiet_seconds
                    dirty_since[path] = min(dirty_since.get(path, due_at), due_at)
                disabled_job = None
                if sub_cfg is not None:
                    for job in sub_cfg.folders:
                        if job.path == path and is_auto_disabled(sub_cfg, job):
                            disabled_job = job
                            break
                if disabled_job is not None:
                    status.update_folder(path, "disabled", error=disabled_job.last_error)
                elif run_error:
                    status.update_folder(path, "error", error=run_error)
                elif summary and summary.get("failed", 0):
                    status.update_folder(
                        path,
                        "error",
                        error=f"{summary.get('failed', 0)} folder run(s) failed",
                        last_summary=summary,
                    )
                else:
                    status.update_folder(path, "idle", error="", last_summary=summary or {})

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
                status.reconcile(
                    (job.path for job in cfg.folders if job.enabled),
                    disabled={job.path: job.last_error for job in auto_disabled_jobs(cfg)},
                )

            do_full_scan = now - last_full_scan >= full_scan_interval
            if do_full_scan:
                last_full_scan = now

            if force or cfg.scheduler_enabled:
                # Events only report changes, so empty folders that already exist when a
                # cleanup-only folder starts being watched need one initial pass.
                with state_lock:
                    for job in cfg.folders:
                        if job.enabled and job.empty_dirs_only and job.path not in primed_cleanup:
                            primed_cleanup.add(job.path)
                            dirty_since.setdefault(job.path, now)
                due_paths = set()
                enabled = [job for job in cfg.folders if job.enabled]
                with state_lock:
                    scan_jobs = [job for job in enabled if job.path not in in_flight]
                scan_paths = {job.path for job in scan_jobs}
                new_fast_sigs = (
                    {job.path: watch_signature_fast(job) for job in scan_jobs}
                    if not use_events else {}
                )
                new_full_sigs = (
                    {job.path: watch_signature(job) for job in scan_jobs}
                    if do_full_scan else {}
                )

                with state_lock:
                    for job in enabled:
                        path = job.path
                        if path in in_flight or path not in scan_paths:
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
                                full_required.add(path)
                            if path in maintenance_due:
                                sig_changed = True
                                full_required.add(path)

                        if sig_changed:
                            dirty_since[path] = now
                            status.update_folder(path, "dirty", quiet_seconds=cfg.watch_quiet_seconds)
                            continue

                        started = dirty_since.get(path)
                        if started is not None and now - started >= cfg.watch_quiet_seconds:
                            due_paths.add(path)
                            del dirty_since[path]
                            in_flight.add(path)
                            changes = monitor.take_changes(path) if use_events else None
                            eligible = (changes and reconciled.get(path) == build_organize_cmd(job)
                                        and path not in full_required
                                        and not job.expand_subfolders
                                        and not job.rules_file and not job.archive_root
                                        and not job.random_names_after_organize
                                        and not job.empty_dirs_only
                                        and job.strategy == "flatten-root")
                            if eligible:
                                incremental_batches[path] = {path: changes}
                            status.update_folder(path, "running")

                for path in due_paths:
                    executor.submit(_organize_path, path)

            if now - last_heartbeat >= 5.0:
                last_heartbeat = now
                status.heartbeat()

            poll_seconds = WATCH_EVENT_TICK_SECONDS if use_events else cfg.watch_poll_seconds
            remaining = poll_seconds
            while remaining > 0:
                if should_stop():
                    return
                step = min(remaining, 1.0)
                time.sleep(step)
                remaining -= step
    finally:
        status.stop("daemon stopped")
        if monitor is not None:
            monitor.stop()
        executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
