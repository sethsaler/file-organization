#!/usr/bin/env python3
"""Install and diagnose an isolated, version-bound native-watching runtime.

Installation is explicit; starting the daemon never downloads packages. A new
runtime is validated before current.json is replaced, leaving an existing
working environment available if installation fails.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
import venv


def runtime_root() -> Path:
    override = os.environ.get("FILE_ORG_RUNTIME_HOME")
    if override:
        return Path(override).expanduser()
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return data / "file-organization/runtime"


def _probe(executable: Path) -> dict:
    try:
        result = subprocess.run(
            [str(executable), "-c", "import json,sys,watchdog; import importlib.metadata; "
             "print(json.dumps({'python':sys.executable,'version':list(sys.version_info[:3]),"
             "'watchdog':importlib.metadata.version('watchdog')}))"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {}


def managed_python(*, verify: bool = True) -> Path | None:
    try:
        record = json.loads((runtime_root() / "current.json").read_text(encoding="utf-8"))
        executable = Path(record["python"])
        # Do not execute a path that escaped the explicitly managed directory.
        executable.resolve().relative_to(runtime_root().resolve())
        if executable.is_file() and (not verify or _probe(executable)):
            return executable
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def runtime_health() -> dict:
    available = importlib.util.find_spec("watchdog") is not None
    selected = managed_python(verify=False)
    current = Path(sys.executable).absolute()
    return {
        "python": str(current),
        "python_version": sys.version.split()[0],
        "watchdog_available": available,
        "managed": selected is not None and selected.absolute() == current,
        "managed_python": str(selected) if selected else None,
        "issue": None if available else "Native watching is unavailable in this Python. Install or repair the scheduler runtime.",
        "repair_command": [sys.executable, str(Path(__file__).resolve()), "--install"],
    }


def install_runtime() -> dict:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / ("python-" + str(sys.version_info.major) + str(sys.version_info.minor) + "-" + uuid.uuid4().hex[:10])
    # Copies bind this environment to the selected interpreter version instead
    # of following Homebrew's mutable python3 symlink into a different minor.
    venv.EnvBuilder(with_pip=True, symlinks=False).create(destination)
    executable = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(executable), "-m", "pip", "install", "--disable-pip-version-check", "watchdog>=3,<8"], check=True)
    result = _probe(executable)
    if not result:
        raise RuntimeError("The new scheduler runtime failed its watchdog import check; existing runtime was retained.")
    result["python"] = str(executable)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".runtime-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(result, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(root / "current.json")
    return result


def activate_managed_runtime() -> None:
    """Re-exec inside the validated runtime, including when a TCC wrapper starts us."""
    if os.environ.get("FILE_ORG_RUNTIME_ACTIVE") == "1":
        return
    executable = managed_python()
    if executable is not None and executable.absolute() != Path(sys.executable).absolute():
        env = dict(os.environ, FILE_ORG_RUNTIME_ACTIVE="1")
        os.execve(str(executable), [str(executable), *sys.argv], env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Create and validate a managed watchdog environment")
    args = parser.parse_args()
    print(json.dumps(install_runtime() if args.install else runtime_health(), indent=2))


if __name__ == "__main__":
    main()
