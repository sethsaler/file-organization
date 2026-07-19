#!/usr/bin/env python3
"""Shared schedule JSON schema, loader/saver, and parallel organizer runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_buckets import bucket_names_for_profile
from org_rules import FALLBACK_BUCKET, VALID_FALLBACKS

# Protects the read-modify-write of schedule.json when multiple threads run folders in parallel.
_config_lock = threading.Lock()
from org_paths import normalize_folder_input


CONFIG_VERSION = 6

SCHEDULE_MODE_INTERVAL = "interval"
SCHEDULE_MODE_DAILY = "daily"
SCHEDULE_MODE_WATCH = "watch"
DEFAULT_DAILY_TIME = "00:00"

# Watch mode: how often the foreground daemon polls folder mtimes (only used by
# the polling fallback when the optional `watchdog` package is not installed),
# and how long a folder must stay quiet after a change before a run fires (lets
# copies finish — in-progress writes keep emitting events/mtime bumps, which
# keeps resetting the quiet timer). These are defaults; both can be overridden
# per-config.
WATCH_POLL_SECONDS = 0.25
WATCH_QUIET_SECONDS = 0.3


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
    expand_subfolders: bool = False
    random_names_after_organize: bool = False
    skip_randomly_renamed: bool = True
    min_unsorted_threshold: int = 0
    detect_duplicates: bool = False
    duplicates_hardlink: bool = False
    date_buckets: bool = False
    rules_file: Optional[str] = None
    unmatched_mode: str = "bucket"
    archive_root: Optional[str] = None
    archive_mapping: Optional[str] = None
    timeout_minutes: int = 60  # 0 = no timeout


def parse_daily_time(value: str) -> Tuple[int, int]:
    """Parse HH:MM (24h local). Invalid input defaults to midnight."""
    raw = (value or DEFAULT_DAILY_TIME).strip()
    parts = raw.split(":")
    if len(parts) != 2:
        return 0, 0
    try:
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1])))
        return hour, minute
    except ValueError:
        return 0, 0


def format_daily_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def normalize_daily_time(value: str) -> str:
    h, m = parse_daily_time(value)
    return format_daily_time(h, m)


def normalize_schedule_mode(value: str) -> str:
    mode = (value or SCHEDULE_MODE_INTERVAL).strip().casefold()
    if mode == SCHEDULE_MODE_DAILY:
        return SCHEDULE_MODE_DAILY
    if mode == SCHEDULE_MODE_WATCH:
        return SCHEDULE_MODE_WATCH
    return SCHEDULE_MODE_INTERVAL


def seconds_until_next_daily_run(
    daily_time: str,
    *,
    now: Optional[datetime] = None,
) -> float:
    """Seconds until the next local-time run at daily_time (HH:MM)."""
    if now is None:
        now = datetime.now().astimezone()
    hour, minute = parse_daily_time(daily_time)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def wait_seconds_after_run(cfg: ScheduleConfig) -> float:
    """How long to sleep after a batch before the next scheduled run."""
    mode = normalize_schedule_mode(cfg.schedule_mode)
    if mode == SCHEDULE_MODE_DAILY:
        return seconds_until_next_daily_run(cfg.daily_time)
    if mode == SCHEDULE_MODE_WATCH:
        return cfg.watch_poll_seconds
    minutes = max(1, min(10080, int(cfg.interval_minutes)))
    return float(minutes * 60)


def watch_signature(job: FolderJob) -> Tuple[float, ...]:
    """Cheap change signature for watch mode: mtimes of the watched root and all
    directories beneath it (one stat per directory, symlinks are not followed).

    A new/removed/modified file changes the mtime of its immediate parent directory,
    so this catches changes anywhere inside the watched tree.
    """
    base = normalize_folder_input(job.path)
    try:
        base_stat = os.stat(base)
    except OSError:
        return (0.0,)

    base_str = str(base)
    entries: List[Tuple[str, float]] = [(base_str, base_stat.st_mtime)]
    stack = [base_str]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            entries.append((entry.path, st.st_mtime))
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue

    # Stable ordering by absolute path; all entries share the same base prefix.
    entries.sort(key=lambda x: x[0])
    return tuple(mtime for _, mtime in entries)


def watch_signature_fast(job: FolderJob) -> Tuple[float, ...]:
    """Lightweight change signature for high-frequency polling.

    Only stats the watched root and its immediate subdirectories. Files dropped at
    the folder root or one level deep change those mtimes, so this catches the
    common case with dramatically less work than the recursive signature. Deep
    changes are still caught by the periodic full scan in the watch loop.
    """
    base = normalize_folder_input(job.path)
    try:
        base_stat = os.stat(base)
    except OSError:
        return (0.0,)

    base_str = str(base)
    entries: List[Tuple[str, float]] = [(base_str, base_stat.st_mtime)]
    try:
        with os.scandir(base_str) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        entries.append((entry.path, st.st_mtime))
                except OSError:
                    continue
    except OSError:
        pass

    entries.sort(key=lambda x: x[0])
    return tuple(mtime for _, mtime in entries)


def estimate_seconds_until_next_run(cfg: ScheduleConfig) -> float:
    """Best-effort countdown until the next scheduled batch (for UI display)."""
    mode = normalize_schedule_mode(cfg.schedule_mode)
    if mode == SCHEDULE_MODE_DAILY:
        return seconds_until_next_daily_run(cfg.daily_time)
    if mode == SCHEDULE_MODE_WATCH:
        return 0.0

    interval_sec = float(max(1, min(10080, int(cfg.interval_minutes))) * 60)
    latest: Optional[datetime] = None
    for job in cfg.folders:
        if not job.enabled or not job.last_run:
            continue
        try:
            raw = job.last_run.replace("Z", "+00:00")
            ts = datetime.fromisoformat(raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    if latest is None:
        return interval_sec
    elapsed = (datetime.now(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds()
    return max(0.0, interval_sec - elapsed)


@dataclass
class ScheduleConfig:
    version: int = CONFIG_VERSION
    schedule_mode: str = SCHEDULE_MODE_INTERVAL
    interval_minutes: int = 60
    daily_time: str = DEFAULT_DAILY_TIME
    scheduler_enabled: bool = False
    max_parallel: int = 0
    max_failures_before_disable: int = 5
    notify_on_run: bool = True
    watch_poll_seconds: float = WATCH_POLL_SECONDS
    watch_quiet_seconds: float = WATCH_QUIET_SECONDS
    folders: List[FolderJob] = field(default_factory=list)

    def to_json_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "version": CONFIG_VERSION,
            "schedule_mode": normalize_schedule_mode(self.schedule_mode),
            "interval_minutes": self.interval_minutes,
            "scheduler_enabled": self.scheduler_enabled,
            "folders": [asdict(f) for f in self.folders],
        }
        if normalize_schedule_mode(self.schedule_mode) == SCHEDULE_MODE_DAILY:
            d["daily_time"] = normalize_daily_time(self.daily_time)
        if self.max_parallel != 0:
            d["max_parallel"] = self.max_parallel
        if self.max_failures_before_disable != 5:
            d["max_failures_before_disable"] = self.max_failures_before_disable
        if not self.notify_on_run:
            d["notify_on_run"] = False
        if self.watch_poll_seconds != WATCH_POLL_SECONDS:
            d["watch_poll_seconds"] = round(self.watch_poll_seconds, 3)
        if self.watch_quiet_seconds != WATCH_QUIET_SECONDS:
            d["watch_quiet_seconds"] = round(self.watch_quiet_seconds, 3)
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
                    expand_subfolders=bool(item.get("expand_subfolders", False)),
                    random_names_after_organize=bool(item.get("random_names_after_organize", False)),
                    skip_randomly_renamed=bool(item.get("skip_randomly_renamed", True)),
                    min_unsorted_threshold=max(0, int(item.get("min_unsorted_threshold", 0))),
                    detect_duplicates=bool(item.get("detect_duplicates", False)),
                    duplicates_hardlink=bool(item.get("duplicates_hardlink", False)),
                    date_buckets=bool(item.get("date_buckets", False)),
                    rules_file=str(item.get("rules_file") or "").strip() or None,
                    unmatched_mode=(
                        str(item.get("unmatched_mode", FALLBACK_BUCKET)).strip().casefold()
                        if str(item.get("unmatched_mode", FALLBACK_BUCKET)).strip().casefold() in VALID_FALLBACKS
                        else FALLBACK_BUCKET
                    ),
                    archive_root=str(item.get("archive_root") or "").strip() or None,
                    archive_mapping=str(item.get("archive_mapping") or "").strip() or None,
                    timeout_minutes=max(0, min(1440, int(item.get("timeout_minutes", 60)))),
                )
            )
        max_parallel = max(0, min(128, int(data.get("max_parallel", 0))))
        mfd = int(data.get("max_failures_before_disable", 5))
        mfd = max(0, min(1000, mfd))
        watch_poll_seconds = float(data.get("watch_poll_seconds", WATCH_POLL_SECONDS))
        watch_quiet_seconds = float(data.get("watch_quiet_seconds", WATCH_QUIET_SECONDS))
        return cls(
            version=max(int(data.get("version", 1)), 1),
            schedule_mode=normalize_schedule_mode(str(data.get("schedule_mode", SCHEDULE_MODE_INTERVAL))),
            interval_minutes=max(1, min(10080, int(data.get("interval_minutes", 60)))),
            daily_time=normalize_daily_time(str(data.get("daily_time", DEFAULT_DAILY_TIME))),
            scheduler_enabled=bool(data.get("scheduler_enabled", False)),
            max_parallel=max_parallel,
            max_failures_before_disable=mfd,
            notify_on_run=bool(data.get("notify_on_run", True)),
            watch_poll_seconds=max(0.05, min(60.0, watch_poll_seconds)),
            watch_quiet_seconds=max(0.0, min(60.0, watch_quiet_seconds)),
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


def _merge_and_save_config(cfg: ScheduleConfig, config_path: Path) -> None:
    """Merge per-run updates back into the latest on-disk config without overwriting concurrent runs."""
    with _config_lock:
        fresh = load_config(config_path)
        updated = {j.path: j for j in cfg.folders}
        for job in fresh.folders:
            if job.path in updated:
                u = updated[job.path]
                job.last_run = u.last_run
                job.last_error = u.last_error
                job.consecutive_failures = u.consecutive_failures
                job.enabled = u.enabled
        save_config(config_path, fresh)


def build_organize_cmd(job: FolderJob, python_executable: Optional[str] = None, *, dry_run: bool = False) -> List[str]:
    py = python_executable or sys.executable
    base = normalize_folder_input(job.path)
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
    if job.random_names_after_organize:
        cmd.append("--random-names-after-organize")
    if job.skip_randomly_renamed:
        cmd.append("--skip-randomly-renamed")
    if job.detect_duplicates:
        cmd.append("--detect-duplicates")
        if job.duplicates_hardlink:
            cmd.append("--duplicates-hardlink")
    if job.date_buckets:
        cmd.append("--date-buckets")
    if job.rules_file and not job.archive_root:
        cmd.extend(["--rules", job.rules_file])
    if job.unmatched_mode and job.unmatched_mode != "bucket":
        cmd.extend(["--unmatched", job.unmatched_mode])
    if job.archive_root:
        cmd.extend(["--archive-root", job.archive_root])
        if job.archive_mapping:
            cmd.extend(["--archive-mapping", job.archive_mapping])
    cmd.append("--backup")
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def find_path_conflicts(cfg: ScheduleConfig) -> List[str]:
    """Return human-readable warnings when enabled folder paths nest or overlap."""
    enabled = [normalize_folder_input(j.path).resolve() for j in cfg.folders if j.enabled]
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


def expand_subfolders(job: FolderJob) -> List[FolderJob]:
    """Expand a job with expand_subfolders=True into one job per subfolder."""
    if not job.expand_subfolders:
        return [job]

    base = normalize_folder_input(job.path).resolve()
    if not base.is_dir():
        return [job]

    subfolders: List[FolderJob] = []
    for item in base.iterdir():
        if item.is_dir():
            sub_job = FolderJob(
                path=str(item),
                enabled=job.enabled,
                recursive=job.recursive,
                strategy=job.strategy,
                normalize=job.normalize,
                include_hidden=job.include_hidden,
                collect_empty_dirs=job.collect_empty_dirs,
                profile=job.profile,
                exclude_defaults=job.exclude_defaults,
                exclude=job.exclude.copy(),
                consecutive_failures=job.consecutive_failures,
                dry_run_verified=job.dry_run_verified,
                last_run=job.last_run,
                last_error=job.last_error,
                expand_subfolders=False,
                random_names_after_organize=job.random_names_after_organize,
                skip_randomly_renamed=job.skip_randomly_renamed,
                min_unsorted_threshold=job.min_unsorted_threshold,
                detect_duplicates=job.detect_duplicates,
                duplicates_hardlink=job.duplicates_hardlink,
                date_buckets=job.date_buckets,
                rules_file=job.rules_file,
                unmatched_mode=job.unmatched_mode,
                archive_root=job.archive_root,
                archive_mapping=job.archive_mapping,
                timeout_minutes=job.timeout_minutes,
            )
            subfolders.append(sub_job)

    return subfolders if subfolders else [job]


_ALWAYS_SKIP_DIRS: Set[str] = {".organizer", "For Deletion", "Duplicates", "Needs Review"}


def count_unsorted_files(job: FolderJob, *, stop_at: int = 0) -> int:
    """Count loose files that would be moved by an organize run.

    For ``flatten-root`` (or non-recursive): counts regular files directly at the
    folder root, excluding ``.DS_Store``.

    For ``in-place`` recursive: walks the tree, skipping directories whose names
    match bucket names (from the job's profile) or ``.organizer`` / ``For Deletion``,
    and counts regular files (excluding ``.DS_Store``) in each remaining directory.

    If *stop_at* > 0, stops early once the count reaches that value.
    """
    base = normalize_folder_input(job.path)
    if not base.is_dir():
        return 0

    bucket_names: Set[str] = set()
    try:
        for name in bucket_names_for_profile(job.profile):
            bucket_names.add(name.casefold())
    except (ValueError, OSError):
        bucket_names = {b.casefold() for b in bucket_names_for_profile("standard")}

    skip_dirs = bucket_names | {d.casefold() for d in _ALWAYS_SKIP_DIRS}

    if (job.strategy == "in-place" and job.recursive) or job.rules_file or job.archive_root:
        count = 0
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d.casefold() not in skip_dirs]
            for fname in files:
                if fname == ".DS_Store":
                    continue
                count += 1
                if stop_at > 0 and count >= stop_at:
                    return count
        return count

    count = 0
    for item in base.iterdir():
        if item.is_file() and item.name != ".DS_Store":
            count += 1
            if stop_at > 0 and count >= stop_at:
                return count
    return count


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
    base = normalize_folder_input(job.path)
    if not base.is_dir():
        ts = datetime.now(timezone.utc).isoformat()
        return idx, ts, "path missing or not a directory", "", "", 1
    timeout_sec = job.timeout_minutes * 60 if job.timeout_minutes > 0 else None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        ts = datetime.now(timezone.utc).isoformat()
        return idx, ts, f"timed out after {job.timeout_minutes} minute(s)", "", "", 1
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
    only_paths: Optional[Set[str]] = None,
) -> Dict[str, int]:
    """Run all runnable enabled folders; returns {"ran", "failed", "skipped"} counts."""
    from org_logging import append_history_entry, append_log_line

    result = {"ran": 0, "failed": 0, "skipped": 0}

    conflicts = find_path_conflicts(cfg)
    if conflicts and log:
        log("Path overlap warnings (runs are not blocked, but order may matter):\n")
        for w in conflicts:
            log(f"  - {w}\n")

    mp = max_parallel if max_parallel is not None else cfg.max_parallel
    tasks: List[Tuple[int, FolderJob, List[str]]] = []
    expanded_jobs: List[Tuple[int, FolderJob]] = []
    
    for idx, job in enumerate(cfg.folders):
        if not job.enabled:
            continue
        if only_paths is not None and job.path not in only_paths:
            continue
        expanded = expand_subfolders(job)
        for sub_job in expanded:
            expanded_jobs.append((idx, sub_job))
    
    for idx, job in expanded_jobs:
        if not job.dry_run_verified:
            if log:
                log(
                    f"\n[skip {job.path}]: dry-run not verified; "
                    "add the folder after a successful preview, or edit schedule.json if you accept the risk.\n"
                )
            if file_log_path:
                append_log_line(file_log_path, f"{job.path}: skipped (dry-run not verified)")
            result["skipped"] += 1
            continue
        if job.min_unsorted_threshold > 0:
            unsorted = count_unsorted_files(job, stop_at=job.min_unsorted_threshold)
            if unsorted < job.min_unsorted_threshold:
                result["skipped"] += 1
                if log:
                    log(
                        f"\n[skip {job.path}]: {unsorted} unsorted file(s) "
                        f"< threshold {job.min_unsorted_threshold}\n"
                    )
                if file_log_path:
                    append_log_line(
                        file_log_path,
                        f"{job.path}: skipped ({unsorted} < {job.min_unsorted_threshold} unsorted)",
                    )
                continue
        cmd = build_organize_cmd(job)
        tasks.append((idx, job, cmd))

    if not tasks:
        return result

    workers = _effective_max_workers(len(tasks), mp)
    msg = f"{label}: {len(tasks)} folder(s), up to {workers} parallel"
    if log:
        log(f"\n--- {msg} ---\n")
    if file_log_path:
        append_log_line(file_log_path, msg)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run_single_job, t): (t[0], t[1]) for t in tasks}
        for fut in as_completed(futures):
            idx, ts, last_err, out, err, rc = fut.result()
            task_job = futures[fut][1]
            result["ran"] += 1
            if last_err:
                result["failed"] += 1
            append_history_entry(_history_record(task_job, label, last_err, out))
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
        _merge_and_save_config(cfg, config_path)
    except OSError:
        pass
    return result


def _history_record(job: FolderJob, label: str, last_err: Optional[str], out: str) -> Dict[str, Any]:
    """Compact per-run history entry; pulls key stats from the organizer's JSON summary."""
    rec: Dict[str, Any] = {"path": job.path, "label": label, "ok": not last_err}
    if last_err:
        rec["error"] = last_err
    start = out.find("{")
    if start != -1:
        try:
            summary = json.loads(out[start:])
        except ValueError:
            return rec
        if isinstance(summary, dict):
            rec["files_moved"] = summary.get("files_moved")
            rec["moved_by_category"] = summary.get("moved_by_category")
            rec["name_collisions_resolved"] = summary.get("name_collisions_resolved")
            rec["backup_manifest"] = summary.get("backup_manifest")
            dup = summary.get("duplicates")
            if isinstance(dup, dict) and dup.get("enabled"):
                rec["duplicates_moved"] = dup.get("files_moved")
            efc = summary.get("empty_folder_collection")
            if isinstance(efc, dict) and efc.get("folders_moved"):
                rec["empty_dirs_staged"] = efc.get("folders_moved")
            routing = summary.get("routing")
            if isinstance(routing, dict):
                rec["needs_review_files"] = routing.get("needs_review_files")
                rec["external_moves"] = routing.get("external_moves")
                rec["matched_by_rule"] = routing.get("matched_by_rule")
    return rec
