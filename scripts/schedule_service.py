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


def _python_executable() -> str:
    return sys.executable


def _log_paths() -> Tuple[Path, Path]:
    state = default_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    return state / "schedule-daemon.log", state / "schedule-daemon.err.log"


def build_launchd_plist() -> dict:
    out_log, err_log = _log_paths()
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            _python_executable(),
            str(daemon_script()),
            "--foreground",
        ],
        "WorkingDirectory": str(_SCRIPT_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(err_log),
    }


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
    ok, msg = install_service_files()
    if not ok:
        return False, msg
    if is_service_running():
        return True, "Background scheduler already running"

    backend = platform_backend()
    if backend == "launchd":
        plist = str(launchd_plist_path())
        domain = f"gui/{os.getuid()}"
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
