#!/usr/bin/env python3
"""Small command surface used by the macOS menu bar and Finder actions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from org_logging import default_log_path, read_history
from org_manifest import restore_from_manifest
from org_watch_status import read_watch_status
from schedule_config import default_config_path, load_config, run_enabled_folders, save_config
from schedule_service import is_service_running, sync_service_enabled


_SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quick controls for File Organizer")
    parser.add_argument(
        "command",
        choices=("status", "pause", "resume", "toggle", "run-all", "undo-latest", "open", "open-folder"),
    )
    parser.add_argument("path", nargs="?", help="Folder for open-folder")
    return parser.parse_args()


def status_payload() -> dict:
    cfg = load_config(default_config_path())
    watch = read_watch_status()
    enabled = [job for job in cfg.folders if job.enabled]
    return {
        "scheduler_enabled": cfg.scheduler_enabled,
        "service_running": is_service_running(),
        "schedule_mode": cfg.schedule_mode,
        "enabled_folders": len(enabled),
        "total_folders": len(cfg.folders),
        "pending": int(watch.get("pending_count", 0) or 0),
        "running": int(watch.get("running_count", 0) or 0),
        "backend": watch.get("backend"),
        "watch_updated_at": watch.get("updated_at"),
        "folders": [job.path for job in enabled],
    }


def set_enabled(enabled: bool) -> tuple[bool, str]:
    path = default_config_path()
    cfg = load_config(path)
    cfg.scheduler_enabled = enabled
    save_config(path, cfg)
    ok, message = sync_service_enabled(enabled)
    return ok, message


def open_app(path: str | None = None) -> tuple[bool, str]:
    cmd = [sys.executable, str(_SCRIPT_DIR / "command_center.py")]
    if path:
        cmd.extend(["--path", path, "--page", "organize"])
    try:
        subprocess.Popen(cmd, cwd=str(_SCRIPT_DIR))
        return True, "File Organizer opened"
    except OSError as exc:
        return False, str(exc)


def main() -> None:
    args = parse_args()
    if args.command == "status":
        print(json.dumps(status_payload(), indent=2))
        return

    if args.command == "pause":
        ok, message = set_enabled(False)
    elif args.command == "resume":
        ok, message = set_enabled(True)
    elif args.command == "toggle":
        current = load_config(default_config_path()).scheduler_enabled
        ok, message = set_enabled(not current)
    elif args.command == "run-all":
        cfg = load_config(default_config_path())
        result = run_enabled_folders(
            cfg,
            default_config_path(),
            label="menu-bar",
            file_log_path=default_log_path(),
        )
        ok = result.get("failed", 0) == 0
        message = f"Ran {result.get('ran', 0)} folder(s); {result.get('failed', 0)} failed"
    elif args.command == "undo-latest":
        records = read_history(50)
        manifest = next(
            (
                str(record.get("backup_manifest"))
                for record in records
                if record.get("backup_manifest") and Path(str(record.get("backup_manifest"))).is_file()
            ),
            "",
        )
        if not manifest:
            ok, message = False, "No recent recovery backup is available"
        else:
            ok = restore_from_manifest(manifest)
            message = "Latest run restored" if ok else "Restore failed"
    elif args.command == "open-folder":
        if not args.path:
            ok, message = False, "open-folder requires a path"
        else:
            ok, message = open_app(args.path)
    else:
        ok, message = open_app()

    print(json.dumps({"ok": ok, "message": message}, indent=2))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
