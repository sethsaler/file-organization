#!/usr/bin/env python3
"""Install and control the background schedule daemon (LaunchAgent / systemd user unit)."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_logging import default_log_path, default_state_dir
from org_paths import normalize_folder_input
from schedule_config import (
    SCHEDULE_MODE_DAILY,
    SCHEDULE_MODE_INTERVAL,
    SCHEDULE_MODE_WATCH,
    default_config_path,
    load_config,
    normalize_schedule_mode,
    parse_daily_time,
)

SERVICE_LABEL = "org.fileorganization.schedule-daemon"
SYSTEMD_UNIT = "file-org-scheduler.service"


def project_root() -> Path:
    return _SCRIPT_DIR.parent


def daemon_script() -> Path:
    return _SCRIPT_DIR / "schedule_daemon.py"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        cfg = Path(base) / "systemd" / "user"
    else:
        cfg = Path.home() / ".config" / "systemd" / "user"
    return cfg / SYSTEMD_UNIT


# Version-stable `python3` symlinks (Apple Silicon / Intel Homebrew). Homebrew
# keeps these pointing at the current install across version bumps, unlike the
# versioned paths sys.executable can report.
_STABLE_PYTHON_CANDIDATES = (
    Path("/opt/homebrew/bin/python3"),
    Path("/usr/local/bin/python3"),
)


def _python_executable() -> str:
    """Interpreter path to bake into service files.

    sys.executable can live under a versioned Homebrew path
    (e.g. /opt/homebrew/opt/python@3.14/bin/python3.14) that disappears on the
    next `brew upgrade`, after which launchd keeps firing the agent but every
    run exits instantly. Prefer a stable symlink that resolves to the same
    interpreter today and keeps working after upgrades.
    """
    exe = Path(sys.executable)
    try:
        real = exe.resolve()
    except OSError:
        return str(exe)
    for candidate in _STABLE_PYTHON_CANDIDATES:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK) and candidate.resolve() == real:
                return str(candidate)
        except OSError:
            continue
    return str(exe)


def _log_paths() -> Tuple[Path, Path]:
    state = default_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    return state / "schedule-daemon.log", state / "schedule-daemon.err.log"


def _caffeinate_path() -> Optional[Path]:
    """Path to macOS `caffeinate`, or None if unavailable (e.g. non-macOS)."""
    candidate = Path("/usr/bin/caffeinate")
    return candidate if candidate.exists() else None


def _current_schedule() -> Tuple[str, str, int]:
    """Best-effort (mode, daily_time, interval_minutes) from saved config."""
    try:
        cfg = load_config(default_config_path())
        return (
            normalize_schedule_mode(cfg.schedule_mode),
            cfg.daily_time,
            max(1, min(10080, int(cfg.interval_minutes))),
        )
    except Exception:
        return SCHEDULE_MODE_DAILY, "00:00", 60


def _enabled_watch_paths() -> list[str]:
    """Absolute paths of enabled scheduled folders, for launchd WatchPaths."""
    try:
        cfg = load_config(default_config_path())
    except Exception:
        return []
    paths: list[str] = []
    for job in cfg.folders:
        if not job.enabled:
            continue
        try:
            paths.append(str(normalize_folder_input(job.path)))
        except Exception:
            continue
    return paths


def build_launchd_plist() -> dict:
    out_log, err_log = _log_paths()
    plist: dict = {
        "Label": SERVICE_LABEL,
        "WorkingDirectory": str(_SCRIPT_DIR),
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(err_log),
    }

    mode, daily_time, interval_minutes = _current_schedule()
    if mode == SCHEDULE_MODE_DAILY:
        # Let launchd own the timing: it fires at the calendar time and, if the Mac
        # was asleep then, runs the missed event once on the next wake (no drift).
        # KeepAlive/RunAtLoad are intentionally omitted so the one-shot run only
        # happens on the schedule rather than every time the agent loads.
        hour, minute = parse_daily_time(daily_time)
        args: list[str] = []
        # Hold a power assertion for the whole run so a scheduled-wake run does not
        # fall back asleep mid-reorganization. caffeinate runs the child and asserts
        # until it exits (-i no idle sleep, -m no disk sleep, -s while on AC power).
        if _caffeinate_path() is not None:
            args += [str(_caffeinate_path()), "-i", "-m", "-s"]
        args += [
            _python_executable(),
            "-u",
            str(daemon_script()),
            "--once",
        ]
        plist["ProgramArguments"] = args
        plist["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
        return plist

    # Watch mode: launchd fires a one-shot run whenever a watched folder changes
    # (WatchPaths is native, event-driven, and needs no resident daemon).
    # ThrottleInterval debounces bursts; a StartInterval backstop still runs
    # hourly in case a change event is missed.
    if mode == SCHEDULE_MODE_WATCH:
        args = []
        if _caffeinate_path() is not None:
            args += [str(_caffeinate_path()), "-i", "-m", "-s"]
        args += [
            _python_executable(),
            "-u",
            str(daemon_script()),
            "--once",
        ]
        plist["ProgramArguments"] = args
        watch_paths = _enabled_watch_paths()
        if watch_paths:
            plist["WatchPaths"] = watch_paths
        plist["ThrottleInterval"] = 15
        plist["StartInterval"] = 3600
        return plist

    # Interval mode with short intervals (≤ 60 min): use StartInterval + --once so
    # launchd fires a fresh one-shot every N seconds without a 24/7 daemon.
    if mode == SCHEDULE_MODE_INTERVAL and interval_minutes <= 60:
        args: list[str] = []
        if _caffeinate_path() is not None:
            args += [str(_caffeinate_path()), "-i", "-m", "-s"]
        args += [
            _python_executable(),
            "-u",
            str(daemon_script()),
            "--once",
        ]
        plist["ProgramArguments"] = args
        plist["StartInterval"] = interval_minutes * 60
        return plist

    # Long interval: keep the long-running loop alive in the background.
    plist["ProgramArguments"] = [
        _python_executable(),
        "-u",
        str(daemon_script()),
        "--foreground",
    ]
    plist["RunAtLoad"] = True
    plist["KeepAlive"] = True
    return plist


def build_systemd_unit() -> str:
    out_log, err_log = _log_paths()
    py = _python_executable()
    daemon = daemon_script()
    return f"""[Unit]
Description=File organization scheduler
After=network.target

[Service]
Type=simple
WorkingDirectory={_SCRIPT_DIR}
ExecStart={py} {daemon} --foreground
Restart=on-failure
RestartSec=30
StandardOutput=append:{out_log}
StandardError=append:{err_log}

[Install]
WantedBy=default.target
"""


def platform_backend() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return "detached"


def is_service_installed() -> bool:
    backend = platform_backend()
    if backend == "launchd":
        return launchd_plist_path().is_file()
    if backend == "systemd":
        return systemd_unit_path().is_file()
    return (default_state_dir() / "schedule-daemon.pid").is_file()


def is_service_running() -> bool:
    backend = platform_backend()
    if backend == "launchd":
        domain = f"gui/{os.getuid()}"
        proc = subprocess.run(
            ["launchctl", "print", f"{domain}/{SERVICE_LABEL}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True
        proc = subprocess.run(
            ["launchctl", "list", SERVICE_LABEL],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0
    if backend == "systemd":
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT],
            capture_output=True,
        )
        return proc.returncode == 0
    pid_path = default_state_dir() / "schedule-daemon.pid"
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _installed_launchd_plist_stale() -> bool:
    """True when the installed agent no longer matches what we would write now.

    Catches the plist referencing a binary that no longer exists (a Homebrew
    Python upgrade or macOS update removed the interpreter baked in at install
    time — launchd keeps firing the agent, but every run exits instantly) as
    well as any other drift from the current config. Callers use this to force
    a reload, since rewriting the plist alone does not affect the loaded agent.
    """
    path = launchd_plist_path()
    try:
        with path.open("rb") as f:
            installed = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return True
    for arg in installed.get("ProgramArguments", []):
        if isinstance(arg, str) and arg.startswith("/") and not Path(arg).exists():
            return True
    return installed != build_launchd_plist()


def install_service_files() -> Tuple[bool, str]:
    if not daemon_script().is_file():
        return False, f"Missing daemon script: {daemon_script()}"

    backend = platform_backend()
    if backend == "launchd":
        path = launchd_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            plistlib.dump(build_launchd_plist(), f)
        return True, f"Wrote {path}"

    if backend == "systemd":
        path = systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_systemd_unit(), encoding="utf-8")
        reload = _run(["systemctl", "--user", "daemon-reload"])
        if reload.returncode != 0:
            err = (reload.stderr or reload.stdout or "").strip()
            return False, f"Wrote {path}, but daemon-reload failed: {err or reload.returncode}"
        return True, f"Wrote {path}"

    return True, "Using detached process (no service file)"


def start_service() -> Tuple[bool, str]:
    backend = platform_backend()
    stale = backend == "launchd" and is_service_installed() and _installed_launchd_plist_stale()

    ok, msg = install_service_files()
    if not ok:
        return False, msg

    if stale and is_service_running():
        # The loaded agent was built from an outdated plist (e.g. its Python was
        # removed by a Homebrew upgrade); unload so the rewritten one gets used.
        plist = str(launchd_plist_path())
        domain = f"gui/{os.getuid()}"
        proc = _run(["launchctl", "bootout", domain, plist])
        if proc.returncode != 0:
            _run(["launchctl", "unload", plist])

    if is_service_running():
        return True, "Background scheduler already running"

    if backend == "launchd":
        plist = str(launchd_plist_path())
        domain = f"gui/{os.getuid()}"
        # Clear any persisted disable (a launchctl disable or a Background Task
        # Management toggle survives reboots and would make bootstrap a no-op).
        _run(["launchctl", "enable", f"{domain}/{SERVICE_LABEL}"])
        proc = _run(["launchctl", "bootstrap", domain, plist])
        if proc.returncode != 0:
            proc = _run(["launchctl", "load", "-w", plist])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"Could not start LaunchAgent: {err or proc.returncode}"
        return True, "Background scheduler started (runs when this app is closed)"

    if backend == "systemd":
        proc = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"Could not start systemd user service: {err or proc.returncode}"
        return True, "Background scheduler started (runs when this app is closed)"

    return _start_detached()


def stop_service() -> Tuple[bool, str]:
    if not is_service_running():
        return True, "Background scheduler already stopped"

    backend = platform_backend()
    if backend == "launchd":
        plist = str(launchd_plist_path())
        domain = f"gui/{os.getuid()}"
        proc = _run(["launchctl", "bootout", domain, plist])
        if proc.returncode != 0:
            proc = _run(["launchctl", "unload", plist])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"Could not stop LaunchAgent: {err or proc.returncode}"
        return True, "Background scheduler stopped"

    if backend == "systemd":
        proc = _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"Could not stop systemd user service: {err or proc.returncode}"
        return True, "Background scheduler stopped"

    return _stop_detached()


def sync_service_enabled(enabled: bool) -> Tuple[bool, str]:
    """Start or stop the background daemon to match scheduler_enabled."""
    if enabled:
        return start_service()
    return stop_service()


def restart_service() -> Tuple[bool, str]:
    """Rewrite the service file and reload it so config changes (mode/daily_time) apply.

    The launchd daily agent bakes the run time into StartCalendarInterval, so a changed
    schedule only takes effect after the agent is reinstalled. No-op when not installed.
    """
    if not is_service_installed() and not is_service_running():
        return True, "Background scheduler not installed"
    stop_service()
    return start_service()


def service_status_line() -> str:
    if is_service_running():
        return "Background scheduler: running"
    if is_service_installed():
        return "Background scheduler: installed but not running"
    return "Background scheduler: not installed"


def service_log_path() -> Path:
    return default_log_path()


def _start_detached() -> Tuple[bool, str]:
    pid_path = default_state_dir() / "schedule-daemon.pid"
    default_state_dir().mkdir(parents=True, exist_ok=True)
    out_log, err_log = _log_paths()
    try:
        with out_log.open("ab") as out_f, err_log.open("ab") as err_f:
            proc = subprocess.Popen(
                [_python_executable(), str(daemon_script()), "--foreground"],
                cwd=str(_SCRIPT_DIR),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
    except OSError as e:
        return False, f"Could not start background scheduler: {e}"
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return True, f"Background scheduler started (pid {proc.pid})"


def _stop_detached() -> Tuple[bool, str]:
    pid_path = default_state_dir() / "schedule-daemon.pid"
    if not pid_path.is_file():
        return True, "Background scheduler already stopped"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return True, "Background scheduler already stopped"
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    pid_path.unlink(missing_ok=True)
    return True, "Background scheduler stopped"
