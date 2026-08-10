#!/usr/bin/env python3
"""Rename all files in a directory tree with random unique sequences."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

# Pattern to detect randomly renamed files: 16 alphanumeric characters before extension
RANDOM_NAME_PATTERN = re.compile(r'^[A-Za-z0-9]{16}\.[^.]+$')


@dataclass
class RenameStats:
    files_renamed: int = 0
    files_skipped: int = 0
    errors: List[str] = field(default_factory=list)


class RandomRenamer:
    def __init__(
        self,
        base: Path,
        recursive: bool = True,
        include_hidden: bool = True,
        dry_run: bool = False,
        verbose: bool = False,
        skip_randomly_renamed: bool = False,
    ) -> None:
        self.base = base
        self.recursive = recursive
        self.include_hidden = include_hidden
        self.dry_run = dry_run
        self.verbose = verbose
        self.skip_randomly_renamed = skip_randomly_renamed
        self.stats = RenameStats()
        self.used_names: Set[str] = set()
        self.renames: List[Dict[str, str]] = []

    def _visible_name(self, name: str) -> bool:
        return self.include_hidden or not name.startswith(".")

    def _is_randomly_renamed(self, filename: str) -> bool:
        """Check if a filename appears to be already randomly renamed."""
        return bool(RANDOM_NAME_PATTERN.match(filename))

    def _generate_random_name(self, extension: str = "") -> str:
        """Generate a unique random filename with the given extension."""
        while True:
            # Generate 16-character random string (letters and digits)
            chars = string.ascii_letters + string.digits
            random_str = ''.join(random.choices(chars, k=16))
            name = random_str + extension
            if name not in self.used_names:
                self.used_names.add(name)
                return name

    def _rename_file(self, src: Path) -> None:
        """Rename a single file with a random name."""
        try:
            # Skip if not a file
            if not src.is_file():
                return

            # Skip if it's a symlink
            if src.is_symlink():
                return

            # Skip files that appear to be already randomly renamed if flag is enabled
            if self.skip_randomly_renamed and self._is_randomly_renamed(src.name):
                self.stats.files_skipped += 1
                return

            # Generate new random name with original extension
            new_name = self._generate_random_name(src.suffix)
            dest = src.parent / new_name

            # Check if destination already exists (shouldn't happen with unique names)
            if dest.exists():
                self.stats.errors.append(f"Destination already exists: {dest}")
                return

            if not self.dry_run:
                src.rename(dest)

            self.renames.append({
                "from": str(src),
                "to": str(dest),
            })
            self.stats.files_renamed += 1

            if self.verbose and self.stats.files_renamed % 100 == 0:
                sys.stderr.write(f"Renamed {self.stats.files_renamed} files…\n")
                sys.stderr.flush()

        except Exception as e:
            self.stats.errors.append(f"Error renaming {src}: {e}")

    def run(self) -> Dict[str, object]:
        """Run the random renaming process."""
        try:
            self.base = self.base.resolve()
        except OSError:
            pass

        if self.recursive:
            # Walk through all subdirectories
            for root, dirs, files in os.walk(self.base, topdown=False):
                root_path = Path(root)

                if not self.include_hidden:
                    dirs[:] = [d for d in dirs if self._visible_name(d)]
                    files = [f for f in files if self._visible_name(f)]

                for filename in files:
                    src = root_path / filename
                    self._rename_file(src)
        else:
            # Only rename files in the root directory
            for item in self.base.iterdir():
                if not self._visible_name(item.name):
                    continue
                self._rename_file(item)

        # Save manifest
        manifest_info = self._save_manifest()

        return {
            "target": str(self.base),
            "recursive": self.recursive,
            "include_hidden": self.include_hidden,
            "dry_run": self.dry_run,
            "files_renamed": self.stats.files_renamed,
            "files_skipped": self.stats.files_skipped,
            "errors": self.stats.errors,
            "backup_manifest": manifest_info.get("manifest") if manifest_info else None,
        }

    def _save_manifest(self) -> Optional[Dict[str, str]]:
        """Save a manifest of all renames for potential restoration."""
        if self.dry_run:
            return None

        from org_manifest import ORGANIZER_DIR_NAME
        from dataclasses import dataclass

        @dataclass
        class RenameEntry:
            from_path: str
            to_path: str

        manifest = {
            "created_at": datetime.now().isoformat(),
            "base_path": str(self.base),
            "operation": "random_rename",
            "recursive": self.recursive,
            "include_hidden": self.include_hidden,
            "renames": self.renames,
        }

        organizer_dir = self.base / ORGANIZER_DIR_NAME
        if not organizer_dir.exists():
            organizer_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        manifest_path = organizer_dir / f"random_rename_manifest_{timestamp}.json"

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        return {
            "manifest": str(manifest_path),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename all files in a directory tree with random unique sequences."
    )
    parser.add_argument("--path", help="Target directory path", required=True)
    parser.add_argument("--recursive", action="store_true", default=True, help="Recursive (default)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Non-recursive (root only)")
    hidden_group = parser.add_mutually_exclusive_group()
    hidden_group.set_defaults(include_hidden=True)
    hidden_group.add_argument("--include-hidden", dest="include_hidden", action="store_true", help="Include hidden files (default)")
    hidden_group.add_argument("--no-include-hidden", dest="include_hidden", action="store_false", help="Exclude dotfiles")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    parser.add_argument("--verbose", action="store_true", help="Progress messages on stderr")
    parser.add_argument("--skip-randomly-renamed", action="store_true", help="Skip files that appear to be already randomly renamed (16-char alphanumeric filenames)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    base = Path(args.path).resolve()
    if not base.exists() or not base.is_dir():
        print(json.dumps({"error": f"Path not found or not a directory: {base}"}, indent=2))
        sys.exit(1)

    renamer = RandomRenamer(
        base=base,
        recursive=args.recursive,
        include_hidden=args.include_hidden,
        dry_run=args.dry_run,
        verbose=args.verbose,
        skip_randomly_renamed=args.skip_randomly_renamed,
    )
    result = renamer.run()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
