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


CONFIG_VERSION = 4


def _helper_script() -> Path:
    return Path(__file__).resolve().parent / "organize_by_filetype.py"


def organizer_script_path() -> Path:
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
    normalize: Optional[str] = None
    include_hidden: bool = True
    collect_empty_dirs: bool = True
    profile: str = "standard"
    exclude_defaults: bool = True
    exclude: List[str] = field(default_factory=list)
    consecutive_failures: int = 0
    dry_run_verified: bool = False
    last_run: Optional[str] = None
    last_error: Optional[str] = None


@dataclass
class ScheduleConfig:
    version: int = CONFIG_VERSION
    interval_minutes: int = 60
    scheduler_enabled: bool = False
    max_parallel: int = 0
    max_failures_before_disable: int = 5
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
        if self.max_failures_before_disable != 5:
            d["max_failures_before_disable"] = self.max_failures_before_disable
        return d

    @classmethod
    def from_json_dict(cls, data: Dict[str, Any]) -> ScheduleConfig:
        folders_raw = data.get("folders") or []
        file_ver = max(int(data.get("version", 1)), 1)
        folders: List[FolderJob] = []
        for item in folders_raw:
            if not isinstance(item, dict):
                continue
            p = str(item.get("path", "")).strip()
            if not p:
                continue
            ex = item.get("exclude") or []
            if not isinstance(ex, list):
                ex = []
            recursive_val = bool(item.get("recursive", True))
            raw_norm = item.get("normalize")
            if raw_norm is None or (isinstance(raw_norm, str) and not raw_norm.strip()):
                normalize_val: Optional[str] = None
            else:
                normalize_val = str(raw_norm).strip()
            if not recursive_val and normalize_val == "standard":
                normalize_val = None
            if file_ver < 4:
                dry_run_verified = True
            else:
                dry_run_verified = bool(item.get("dry_run_verified", False))
            folders.append(
                FolderJob(
                    path=p,
                    enabled=bool(item.get("enabled", True)),
                    recursive=recursive_val,
                    strategy=str(item.get("strategy", "flatten-root")),
                    normalize=normalize_val,
                    include_hidden=bool(item.get("include_hidden", True)),
                    collect_empty_dirs=bool(item.get("collect_empty_dirs", True)),
                    profile=str(item.get("profile", "standard")),
                    exclude_defaults=bool(item.get("exclude_defaults", True)),
                    exclude=[str(x) for x in ex],
                    consecutive_failures=int(item.get("consecutive_failures", 0)),
                    dry_run_verified=dry_run_verified,
                    last_run=item.get("last_run"),
                    last_error=item.get("last_error"),
                )
            )
        max_parallel = max(0, min(128, int(data.get("max_parallel", 0))))
        mfd = int(data.get("max_failures_before_disable", 5))
        mfd = max(0, min(1000, mfd))
        return cls(
            version=max(int(data.get("version", 1)), 1),
            interval_minutes=max(1, min(10080, int(data.get("interval_minutes", 60)))),
            scheduler_enabled=bool(data.get("scheduler_enabled", False)),
            max_parallel=max_parallel,
            max_failures_before_disable=mfd,
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


def effective_normalize(job: FolderJob) -> str:
    """Match organize_by_filetype: standard when recursive, none otherwise, unless explicitly set."""
    if job.normalize:
        return job.normalize
    return "standard" if job.recursive else "none"


def save_config(path: Path, cfg: ScheduleConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_json_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def build_organize_cmd(job: FolderJob, python_executable: Optional[str] = None, *, dry_run: bool = False) -> List[str]:
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
        effective_normalize(job),
        "--profile",
        job.profile,
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
    if job.exclude_defaults:
        cmd.append("--exclude-defaults")
    for pat in job.exclude:
        if pat.strip():
            cmd.extend(["--exclude", pat.strip()])
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def find_path_conflicts(cfg: ScheduleConfig) -> List[str]:
    """Return human-readable warnings when enabled folder paths nest or overlap."""
    enabled = [Path(j.path).expanduser().resolve() for j in cfg.folders if j.enabled]
    warnings: List[str] = []
    for i, a in enumerate(enabled):
        for b in enabled[i + 1 :]:
            try:
                a.relative_to(b)
                warnings.append(f"Nested paths: {a} is inside {b}")
            except ValueError:
                pass
            try:
                b.relative_to(a)
                warnings.append(f"Nested paths: {b} is inside {a}")
            except ValueError:
                pass
    return warnings


def run_dry_run_preview(job: FolderJob) -> Tuple[bool, str]:
    cmd = build_organize_cmd(job, dry_run=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, (proc.stderr or out or f"exit {proc.returncode}")
    return True, out


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
        return idx, ts, "path missing or not a directory", "", "", 1
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        ts = datetime.now(timezone.utc).isoformat()
        return idx, ts, "timed out after 1 hour", "", "", 1
    except OSError as e:
        ts = datetime.now(timezone.utc).isoformat()
        return idx, ts, str(e), "", "", 1
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
    file_log_path: Optional[Path] = None,
) -> None:
    from org_logging import append_log_line

    conflicts = find_path_conflicts(cfg)
    if conflicts and log:
        log("Path overlap warnings (runs are not blocked, but order may matter):\n")
        for w in conflicts:
            log(f"  - {w}\n")

    mp = max_parallel if max_parallel is not None else cfg.max_parallel
    tasks: List[Tuple[int, FolderJob, List[str]]] = []
    for idx, job in enumerate(cfg.folders):
        if not job.enabled:
            continue
        if not job.dry_run_verified:
            if log:
                log(
                    f"\n[skip {job.path}]: dry-run not verified; "
                    "add the folder after a successful preview, or edit schedule.json if you accept the risk.\n"
                )
            if file_log_path:
                append_log_line(file_log_path, f"{job.path}: skipped (dry-run not verified)")
            continue
        cmd = build_organize_cmd(job)
        tasks.append((idx, job, cmd))

    if not tasks:
        return

    workers = _effective_max_workers(len(tasks), mp)
    msg = f"{label}: {len(tasks)} folder(s), up to {workers} parallel"
    if log:
        log(f"\n--- {msg} ---\n")
    if file_log_path:
        append_log_line(file_log_path, msg)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run_single_job, t): t[0] for t in tasks}
        for fut in as_completed(futures):
            idx, ts, last_err, out, err, rc = fut.result()
            if 0 <= idx < len(cfg.folders):
                job = cfg.folders[idx]
                job.last_run = ts
                job.last_error = last_err
                if last_err:
                    job.consecutive_failures += 1
                    cap = cfg.max_failures_before_disable
                    if cap > 0 and job.consecutive_failures >= cap:
                        job.enabled = False
                        job.last_error = f"{last_err} (disabled after {job.consecutive_failures} failures)"
                else:
                    job.consecutive_failures = 0
            if log:
                path_disp = cfg.folders[idx].path if 0 <= idx < len(cfg.folders) else "?"
                log(f"\n[{path_disp}]\n")
                if err:
                    log(err + "\n")
                if out:
                    snippet = out if len(out) < 4000 else out[:4000] + "\n…(truncated)\n"
                    log(snippet + "\n")
                if rc != 0:
                    log(f"(exit {rc})\n")
            if file_log_path:
                status = "ok" if not last_err else f"error: {last_err}"
                path_log = cfg.folders[idx].path if 0 <= idx < len(cfg.folders) else "?"
                append_log_line(file_log_path, f"{path_log}: {status}")

    try:
        save_config(config_path, cfg)
    except OSError:
        pass
