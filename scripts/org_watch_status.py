#!/usr/bin/env python3
"""Atomic watch-daemon health snapshots consumed by the command center."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from org_logging import default_state_dir


STATUS_VERSION = 1


def watch_status_path() -> Path:
    return default_state_dir() / "watch-status.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_watch_status(payload: Mapping[str, Any]) -> None:
    path = watch_status_path()
    data = dict(payload)
    data["version"] = STATUS_VERSION
    data["updated_at"] = utc_now()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def read_watch_status() -> Dict[str, Any]:
    path = watch_status_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class WatchStatus:
    """Small mutable snapshot with rate-limited persistence owned by the daemon."""

    def __init__(
        self,
        *,
        backend: str,
        poll_seconds: float,
        quiet_seconds: float,
        full_scan_seconds: float,
        max_workers: int,
        folders: Iterable[str],
        disabled: Optional[Mapping[str, Optional[str]]] = None,
    ) -> None:
        self._last_flush = 0.0
        self.data: Dict[str, Any] = {
            "active": True,
            "backend": backend,
            "poll_seconds": poll_seconds,
            "quiet_seconds": quiet_seconds,
            "full_scan_seconds": full_scan_seconds,
            "max_workers": max_workers,
            "started_at": utc_now(),
            "folders": {path: {"state": "idle"} for path in folders},
        }
        self.reconcile(list(self.data["folders"]), disabled=disabled)
        self.flush(force=True)

    def reconcile(
        self,
        paths: Iterable[str],
        disabled: Optional[Mapping[str, Optional[str]]] = None,
    ) -> None:
        """Keep exactly the active folders plus any folders the scheduler switched off.

        Folders in `disabled` (path -> last error) stay visible in the snapshot with state
        "disabled" so the command center and menu-bar helper can show that the daemon is up
        but no longer organizing them, instead of silently dropping them.
        """
        folders = self.data.setdefault("folders", {})
        before = json.dumps(folders, sort_keys=True)
        active = set(paths)
        off = dict(disabled or {})
        for path in list(folders):
            if path not in active and path not in off:
                del folders[path]
        for path in active:
            record = folders.setdefault(path, {"state": "idle"})
            if record.get("state") == "disabled":
                record["state"] = "idle"
                record.pop("error", None)
        for path, error in off.items():
            record = folders.setdefault(path, {})
            record["state"] = "disabled"
            if error:
                record["error"] = error
        if json.dumps(folders, sort_keys=True) != before:
            self.flush(force=True)

    def update_folder(self, path: str, state: str, **details: Any) -> None:
        folders = self.data.setdefault("folders", {})
        record = folders.setdefault(path, {})
        previous_state = record.get("state")
        record["state"] = state
        record.update({key: value for key, value in details.items() if value is not None})
        if state == "dirty":
            record["last_event"] = utc_now()
        elif state == "running":
            record["run_started"] = utc_now()
        elif state in {"idle", "error"}:
            record["last_run"] = utc_now()
        self.flush(force=previous_state != state)

    def heartbeat(self) -> None:
        self.flush(force=True)

    def stop(self, reason: str = "stopped") -> None:
        self.data["active"] = False
        self.data["stop_reason"] = reason
        self.flush(force=True)

    def flush(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < 0.25:
            return
        folders = self.data.get("folders") or {}
        self.data["pending_count"] = sum(
            1 for record in folders.values() if record.get("state") in {"dirty", "waiting"}
        )
        self.data["running_count"] = sum(
            1 for record in folders.values() if record.get("state") == "running"
        )
        self.data["disabled_count"] = sum(
            1 for record in folders.values() if record.get("state") == "disabled"
        )
        write_watch_status(self.data)
        self._last_flush = now
