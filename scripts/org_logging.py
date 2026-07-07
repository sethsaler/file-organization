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


def default_history_path() -> Path:
    return default_state_dir() / "history.jsonl"


_HISTORY_MAX_BYTES = 1 << 20  # trim when the file passes ~1 MiB
_HISTORY_KEEP_LINES = 500


def append_history_entry(entry: dict) -> None:
    """Append one per-run record to the JSONL history (best-effort)."""
    import json

    path = default_history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        if path.stat().st_size > _HISTORY_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            path.write_text("".join(lines[-_HISTORY_KEEP_LINES:]), encoding="utf-8")
    except OSError:
        pass


def read_history(limit: int = 100) -> list:
    """Most-recent-first history records; unparseable lines are skipped."""
    import json

    path = default_history_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
