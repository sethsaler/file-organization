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
import time
from pathlib import Path
from typing import Dict, Optional, Set

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_logging import default_log_path, default_state_dir
from schedule_config import (
    SCHEDULE_MODE_WATCH,
    WATCH_POLL_SECONDS,
    WATCH_QUIET_SECONDS,
    default_config_path,
    load_config,
    normalize_schedule_mode,
    run_enabled_folders,
    wait_seconds_after_run,
    watch_signature,
)

WATCH_CONFIG_RELOAD_SECONDS = 30.0


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

    Polls a cheap mtime signature (folder root + immediate subdirs) every
    WATCH_POLL_SECONDS; when a folder changes and then stays quiet for
    WATCH_QUIET_SECONDS, only that folder is organized. Falls back to the
    normal loop if the config's schedule_mode changes away from watch.
    """
    signatures: dict = {}
    dirty_since: dict = {}
    last_reload = 0.0
    cfg = load_config(config_path)

    print(json.dumps({"watch": True, "poll_seconds": WATCH_POLL_SECONDS, "quiet_seconds": WATCH_QUIET_SECONDS}, indent=2))

    while not should_stop():
        now = time.monotonic()
        if now - last_reload >= WATCH_CONFIG_RELOAD_SECONDS:
            cfg = load_config(config_path)
            last_reload = now
            if normalize_schedule_mode(cfg.schedule_mode) != SCHEDULE_MODE_WATCH:
                print(json.dumps({"watch": False, "reason": "schedule_mode changed"}, indent=2))
                return

        if force or cfg.scheduler_enabled:
            due_paths = set()
            for job in cfg.folders:
                if not job.enabled:
                    continue
                sig = watch_signature(job)
                prev = signatures.get(job.path)
                signatures[job.path] = sig
                if prev is None:
                    continue  # first observation is the baseline, not a change
                if sig != prev:
                    dirty_since[job.path] = now
                    continue
                started = dirty_since.get(job.path)
                if started is not None and now - started >= WATCH_QUIET_SECONDS:
                    due_paths.add(job.path)
                    del dirty_since[job.path]

            if due_paths:
                summary = run_enabled_folders(
                    cfg,
                    config_path,
                    max_parallel=max_parallel,
                    log=lambda s: print(s, end=""),
                    label="watch",
                    file_log_path=default_log_path(),
                    only_paths=due_paths,
                )
                _notify_run(cfg, summary, "watch")
                cfg = load_config(config_path)
                last_reload = time.monotonic()
                # The run itself changed the folders; refresh baselines so the
                # organizer's own moves do not retrigger a run.
                for job in cfg.folders:
                    if job.path in due_paths:
                        signatures[job.path] = watch_signature(job)

        remaining = WATCH_POLL_SECONDS
        while remaining > 0:
            if should_stop():
                return
            step = min(remaining, 1.0)
            time.sleep(step)
            remaining -= step


if __name__ == "__main__":
    main()
