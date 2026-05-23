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
import sys
import time
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_logging import default_log_path
from schedule_config import (
    default_config_path,
    load_config,
    run_enabled_folders,
    wait_seconds_after_run,
)


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
        run_enabled_folders(
            cfg,
            config_path,
            max_parallel=args.max_parallel,
            log=lambda s: print(s, end=""),
            label="once",
            file_log_path=default_log_path(),
        )
        print(json.dumps({"ok": True, "config": str(config_path)}, indent=2))
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
        if args.interval_override is not None:
            wait_sec = max(60.0, float(max(1, min(10080, int(args.interval_override))) * 60)
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
            run_enabled_folders(
                cfg,
                config_path,
                max_parallel=args.max_parallel,
                log=lambda s: print(s, end=""),
                label="scheduled",
                file_log_path=default_log_path(),
            )

        first = False
        cfg = load_config(config_path)
        if args.interval_override is not None:
            wait_sec = max(60.0, float(max(1, min(10080, int(args.interval_override))) * 60)
        else:
            wait_sec = wait_seconds_after_run(cfg)
        remaining = wait_sec
        while remaining > 0:
            if stop:
                return
            step = min(remaining, 1.0)
            time.sleep(step)
            remaining -= step


if __name__ == "__main__":
    main()
