"""Run the complete lint and test suite on the selected Python interpreter."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    missing = [
        name for name in ("pyflakes", "pytest", "watchdog")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        print("Missing verification dependencies: " + ", ".join(missing), file=sys.stderr)
        print('Install once: python3 -m pip install ".[dev,watch]" pyflakes', file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="file-org-verify-") as temporary:
        env = dict(os.environ, XDG_CONFIG_HOME=temporary + "/config",
                   XDG_STATE_HOME=temporary + "/state")
        for command in (
            [sys.executable, "-m", "pyflakes", "scripts/", "tests/"],
            [sys.executable, "-m", "pytest", "tests/", "-q"],
        ):
            print("+ " + " ".join(command), flush=True)
            result = subprocess.run(command, cwd=root, env=env)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
