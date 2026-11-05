#!/usr/bin/env python3
"""Small command surface used by the macOS menu bar and Finder actions."""

from __future__ import annotations

import argparse
import copy
import contextlib
import threading
from datetime import datetime, timezone
import json
import subprocess
import sys
from pathlib import Path

from org_logging import default_log_path, read_history
from org_manifest import restore_from_manifest
from org_watch_status import read_watch_status
from schedule_config import auto_disabled_jobs, default_config_path, load_config, run_enabled_folders, merge_config_edits
from schedule_service import is_service_running, restart_service, sync_service_enabled


_SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quick controls for File Organizer")
    parser.add_argument(
        "command",
        choices=("status", "menu-status", "pause", "resume", "toggle", "run-all", "undo-latest", "open", "open-folder"),
    )
    parser.add_argument("path", nargs="?", help="Folder for open-folder")
    return parser.parse_args()


def watch_health_summary(cfg, watch: dict, service_running=None, *, now=None) -> dict:
    """One mode-aware health interpretation shared by menu and windows."""
    now = now or datetime.now(timezone.utc)
    age = None
    try:
        stamp = datetime.fromisoformat(str(watch.get("updated_at") or "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = max(0, (now - stamp).total_seconds())
    except (ValueError, TypeError):
        pass
    stale = age is None or age > max(30, 2 * float(watch.get("full_scan_seconds") or 5))
    disabled = len(auto_disabled_jobs(cfg))
    missing = len(watch.get("missing_watch_paths") or [])
    enabled = sum(job.enabled for job in cfg.folders)
    mode = cfg.schedule_mode
    if not cfg.scheduler_enabled:
        state, message = "paused", "Automatic organization paused"
    elif service_running is False:
        state, message = "unavailable", "Background service unavailable"
    elif mode == "watch" and (stale or not watch.get("active")):
        state, message = "stale", "Watcher health is out of date — open Watched folders"
    elif mode == "watch":
        state, message = "watching", f"Watching {enabled} folder{'s' if enabled != 1 else ''}"
    elif mode == "daily":
        state, message = "scheduled", f"Scheduled daily at {cfg.daily_time}"
    else:
        state, message = "scheduled", f"Scheduled every {cfg.interval_minutes} minutes"
    if disabled:
        message += f" · {disabled} disabled after errors"
    if missing:
        message += f" · {missing} native watches unavailable"
    return {"health_state": state, "health_message": message,
            "watch_stale": stale if mode == "watch" else False,
            "auto_disabled_count": disabled, "missing_watch_count": missing}


def status_payload(*, include_service: bool = True) -> dict:
    cfg = load_config(default_config_path())
    watch = read_watch_status()
    enabled = [job for job in cfg.folders if job.enabled]
    payload = {
        "scheduler_enabled": cfg.scheduler_enabled,
        "schedule_mode": cfg.schedule_mode,
        "enabled_folders": len(enabled),
        "total_folders": len(cfg.folders),
        "pending": int(watch.get("pending_count", 0) or 0),
        "running": int(watch.get("running_count", 0) or 0),
        "backend": watch.get("backend"),
        "watch_updated_at": watch.get("updated_at"),
        "folders": [job.path for job in enabled],
        "auto_disabled_folders": [job.path for job in auto_disabled_jobs(cfg)],
    }
    running = is_service_running() if include_service else None
    if include_service:
        payload["service_running"] = running
    payload["runtime"] = watch.get("runtime")
    payload.update(watch_health_summary(cfg, watch, running))
    return payload


_service_thread_lock = threading.Lock()


@contextlib.contextmanager
def _service_action_lock(config_path: Path):
    """Order menu/window actions across threads and processes."""
    with _service_thread_lock:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with config_path.with_suffix(".service-control.lock").open("a+") as stream:
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _service_intent(cfg):
    return (cfg.scheduler_enabled, cfg.schedule_mode, cfg.daily_time, cfg.interval_minutes)


def reconcile_background_service(config_path=None, *, restart=False):
    """Apply latest saved intent; repair a concurrent change before releasing the lock."""
    path = config_path or default_config_path()
    with _service_action_lock(path):
        for _ in range(8):
            cfg = load_config(path)
            intent = _service_intent(cfg)
            if cfg.scheduler_enabled and restart:
                result = restart_service()
            else:
                result = sync_service_enabled(cfg.scheduler_enabled)
            current = load_config(path)
            if _service_intent(current) == intent:
                return result
            restart = current.scheduler_enabled and _service_intent(current)[1:] != intent[1:]
        return False, "Schedule changed repeatedly; check the latest background service status."


def set_enabled(enabled: bool) -> tuple[bool, str]:
    path = default_config_path()
    cfg = load_config(path)
    baseline = copy.deepcopy(cfg)
    cfg.scheduler_enabled = enabled
    merge_config_edits(path, baseline, cfg)
    ok, message = reconcile_background_service(path)
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
    if args.command in {"status", "menu-status"}:
        print(json.dumps(status_payload(include_service=True), indent=2))
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
