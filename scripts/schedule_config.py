#!/usr/bin/env python3
"""Shared schedule JSON schema, loader/saver, and parallel organizer runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


CONFIG_VERSION = 2


def _helper_script() -> Path:
    return Path(__file__).resolve().parent / "organize_by_filetype.py"


def organizer_script_path() -> Path:
    """Path to organize_by_filetype.py (same directory as this module)."""
    return _helper_script()


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        cfg = Path(base) / "file-organization"
    else:
        cfg = Path.home() / ".config" / "file-organization"
    return cfg / "schedule.json"


@dataclass
class FolderJob:
    path: str
    enabled: bool = True
    recursive: bool = True
    strategy: str = "flatten-root"
    normalize: str = "standard"
    include_hidden: bool = True
    collect_empty_dirs: bool = True
    last_run: Optional[str] = None
    last_error: Optional[str] = None


@dataclass
class ScheduleConfig:
    version: int = CONFIG_VERSION
    interval_minutes: int = 60
    scheduler_enabled: bool = False
    """When >0, cap concurrent organizer subprocesses. When 0, run all enabled folders at once (capped at 32)."""
    max_parallel: int = 0
    folders: List[FolderJob] = field(default_factory=list)

    def to_json_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "version": CONFIG_VERSION,
            "interval_minutes": self.interval_minutes,
            "scheduler_enabled": self.scheduler_enabled,
            "folders": [asdict(f) for f in self.folders],
        }
        if self.max_parallel != 0:
            d["max_parallel"] = self.max_parallel
        return d

    @classmethod
    def from_json_dict(cls, data: Dict[str, Any]) -> ScheduleConfig:
        ver = int(data.get("version", 1))
        folders_raw = data.get("folders") or []
        folders: List[FolderJob] = []
        for item in folders_raw:
            if not isinstance(item, dict):
                continue
            p = str(item.get("path", "")).strip()
            if not p:
                continue
            folders.append(
                FolderJob(
                    path=p,
                    enabled=bool(item.get("enabled", True)),
                    recursive=bool(item.get("recursive", True)),
                    strategy=str(item.get("strategy", "flatten-root")),
                    normalize=str(item.get("normalize", "standard")),
                    include_hidden=bool(item.get("include_hidden", True)),
                    collect_empty_dirs=bool(item.get("collect_empty_dirs", True)),
                    last_run=item.get("last_run"),
                    last_error=item.get("last_error"),
                )
            )
        max_parallel = int(data.get("max_parallel", 0))
        max_parallel = max(0, min(128, max_parallel))
        return cls(
            version=max(ver, 1),
            interval_minutes=max(1, min(10080, int(data.get("interval_minutes", 60)))),
            scheduler_enabled=bool(data.get("scheduler_enabled", False)),
            max_parallel=max_parallel,
            folders=folders,
        )


def load_config(path: Path) -> ScheduleConfig:
    if not path.is_file():
        return ScheduleConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ScheduleConfig()
        return ScheduleConfig.from_json_dict(data)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return ScheduleConfig()


def save_config(path: Path, cfg: ScheduleConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_json_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def build_organize_cmd(job: FolderJob, python_executable: Optional[str] = None) -> List[str]:
    py = python_executable or sys.executable
    base = Path(job.path).expanduser()
    cmd = [
        py,
        str(_helper_script()),
        "--path",
        str(base),
        "--strategy",
        job.strategy,
        "--normalize",
        job.normalize,
    ]
    if job.recursive:
        cmd.append("--recursive")
    else:
        cmd.append("--no-recursive")
    if not job.include_hidden:
        cmd.append("--no-include-hidden")
    if job.collect_empty_dirs:
        cmd.append("--collect-empty-dirs")
    else:
        cmd.append("--no-collect-empty-dirs")
    return cmd


def _effective_max_workers(n_jobs: int, max_parallel: int) -> int:
    if n_jobs <= 0:
        return 1
    if max_parallel > 0:
        return max(1, min(max_parallel, n_jobs, 128))
    return max(1, min(n_jobs, 32))


def _run_single_job(args: Tuple[int, FolderJob, List[str]]) -> Tuple[int, str, Optional[str], str, str, int]:
    idx, job, cmd = args
    base = Path(job.path).expanduser()
    if not base.is_dir():
        ts = datetime.now(timezone.utc).isoformat()
        return idx, ts, "path missing or not a directory", "", "", 0
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        ts = datetime.now(timezone.utc).isoformat()
        return idx, ts, "timed out after 1 hour", "", "", 0
    except OSError as e:
        ts = datetime.now(timezone.utc).isoformat()
        return idx, ts, str(e), "", "", 0
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    ts = datetime.now(timezone.utc).isoformat()
    if proc.returncode != 0:
        return idx, ts, err or f"exit {proc.returncode}", out, err, proc.returncode
    return idx, ts, None, out, err, proc.returncode


def run_enabled_folders(
    cfg: ScheduleConfig,
    config_path: Path,
    *,
    max_parallel: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
    label: str = "run",
) -> None:
    """Run organizer for every enabled folder in parallel; update last_run / last_error and save."""
    mp = max_parallel if max_parallel is not None else cfg.max_parallel
    tasks: List[Tuple[int, FolderJob, List[str]]] = []
    for idx, job in enumerate(cfg.folders):
        if not job.enabled:
            continue
        cmd = build_organize_cmd(job)
        tasks.append((idx, job, cmd))

    if not tasks:
        return

    workers = _effective_max_workers(len(tasks), mp)
    if log:
        log(f"\n--- {label} ({len(tasks)} folder(s), up to {workers} at a time) ---\n")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run_single_job, t): t[0] for t in tasks}
        for fut in as_completed(futures):
            idx, ts, last_err, out, err, rc = fut.result()
            if 0 <= idx < len(cfg.folders):
                cfg.folders[idx].last_run = ts
                cfg.folders[idx].last_error = last_err
            if log:
                t = next((x for x in tasks if x[0] == idx), None)
                cmd_s = " ".join(t[2]) if t else ""
                path_disp = cfg.folders[idx].path if 0 <= idx < len(cfg.folders) else "?"
                log(f"\n[{path_disp}]\n$ {cmd_s}\n")
                if err:
                    log(err + "\n")
                if out:
                    snippet = out if len(out) < 4000 else out[:4000] + "\n…(truncated)\n"
                    log(snippet + "\n")
                if rc != 0:
                    log(f"(exit {rc})\n")

    try:
        save_config(config_path, cfg)
    except OSError:
        pass
