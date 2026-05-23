#!/usr/bin/env python3
"""Cross-platform restore from an organizer backup manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_manifest import list_manifests, restore_from_manifest


def main() -> None:
    p = argparse.ArgumentParser(description="Restore files from an organizer backup manifest.")
    p.add_argument("manifest", nargs="?", help="Path to backup_*.json manifest")
    p.add_argument("--list", metavar="BASE", help="List recent manifests under BASE/.organizer")
    args = p.parse_args()

    if args.list:
        base = Path(args.list).expanduser()
        for m in list_manifests(base):
            print(m)
        return

    if not args.manifest:
        p.error("Provide a manifest path or use --list BASE")

    if not restore_from_manifest(args.manifest):
        sys.exit(1)


if __name__ == "__main__":
    main()
