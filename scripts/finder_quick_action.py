#!/usr/bin/env python3
"""Open a Finder selection in File Organizer's preview-first workflow."""

from __future__ import annotations

import sys
from pathlib import Path

from quick_controls import open_app


def selected_folder(arguments: list[str]) -> Path | None:
    for raw in arguments:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate
        if candidate.is_file():
            return candidate.parent
    return None


def main() -> None:
    folder = selected_folder(sys.argv[1:])
    if folder is None:
        raise SystemExit("Select a file or folder in Finder first")
    ok, message = open_app(str(folder.resolve()))
    if not ok:
        raise SystemExit(message)


if __name__ == "__main__":
    main()
