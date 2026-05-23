#!/usr/bin/env python3
"""Shared logging paths for scheduler and daemon."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if base := xdg:
        return Path(base) / "file-organization"
    return Path.home() / ".local" / "state" / "file-organization"


def default_log_path() -> Path:
    return default_state_dir() / "scheduler.log"


def append_log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}")
        if not message.endswith("\n"):
            f.write("\n")
