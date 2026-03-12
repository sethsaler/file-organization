#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set


ALIAS_MAP_DEFAULT: Dict[str, str] = {
    "JPEG": "JPG",
    "JPE": "JPG",
}


@dataclass
class MoveStats:
    files_moved: int = 0
    name_collisions_resolved: int = 0
    folders_touched: int = 0


@dataclass
class NormalizeStats:
    folders_case_renamed: int = 0
    folders_merged: int = 0
    items_moved_in_merges: int = 0
    merge_collisions_resolved: int = 0
    source_folders_removed: int = 0


class Organizer:
    def __init__(
        self,
        base: Path,
        recursive: bool,
        strategy: str,
        include_hidden: bool,
        normalize: str,
        dry_run: bool,
    ) -> None:
        self.base = base
        self.recursive = recursive
        self.strategy = strategy
        self.include_hidden = include_hidden
        self.normalize = normalize
        self.dry_run = dry_run

        self.alias_map = dict(ALIAS_MAP_DEFAULT)
        self.ext_counts = Counter()
        self.move_stats = MoveStats()
        self.normalize_stats = NormalizeStats()

        # Destination reservation map used for deterministic collision-safe naming.
        # Helps keep behavior predictable, especially in dry-run mode.
        self.reserved_names: Dict[Path, Set[str]] = defaultdict(set)

        self.bucket_names: Set[str] = set()

    def _visible_name(self, name: str) -> bool:
        return self.include_hidden or not name.startswith(".")

    def _collect_extensions(self) -> Set[str]:
        exts: Set[str] = set()
        for root, dirs, files in os.walk(self.base, topdown=True):
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
            for fn in files:
                if not self._visible_name(fn):
                    continue
                suffix = Path(fn).suffix
                ext = suffix[1:].upper() if suffix else "NO_EXTENSION"
                exts.add(ext)
        return exts

    def _canonical_folder_name(self, name: str) -> Optional[str]:
        u = name.upper()
        if u in self.alias_map:
            return self.alias_map[u]
        if u in self.bucket_names:
            return u
        return None

    def _init_reserved_dir(self, directory: Path) -> None:
        if directory not in self.reserved_names:
            existing = set()
            if directory.exists():
                try:
                    existing = {p.name for p in directory.iterdir()}
                except Exception:
                    existing = set()
            self.reserved_names[directory] = existing

    def _collision_safe_target(self, dest_dir: Path, original_name: str) -> Path:
        self._init_reserved_dir(dest_dir)
        reserved = self.reserved_names[dest_dir]

        if original_name not in reserved:
            reserved.add(original_name)
            return dest_dir / original_name

        self.move_stats.name_collisions_resolved += 1
        p = Path(original_name)
        stem, suffix = p.stem, p.suffix

        i = 1
        while True:
            candidate = f"{stem}_{i}{suffix}" if suffix else f"{original_name}_{i}"
            if candidate not in reserved:
                reserved.add(candidate)
                return dest_dir / candidate
            i += 1

    def _bucket_for_file(self, file_name: str) -> str:
        suffix = Path(file_name).suffix
        ext = suffix[1:].upper() if suffix else "NO_EXTENSION"
        return self.alias_map.get(ext, ext)

    def _move_one_file(self, src: Path, dest_dir: Path) -> None:
        dest_dir_name = dest_dir.name
        self.ext_counts[dest_dir_name] += 1

        if not self.dry_run and not dest_dir.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
        elif self.dry_run:
            # Reserve directory path in dry-run bookkeeping.
            self._init_reserved_dir(dest_dir)

        target = self._collision_safe_target(dest_dir, src.name)

        if not self.dry_run:
            shutil.move(str(src), str(target))

        self.move_stats.files_moved += 1

    def _run_non_recursive(self) -> None:
        touched = False
        for p in list(self.base.iterdir()):
            if not p.is_file() or not self._visible_name(p.name):
                continue
            bucket = self._bucket_for_file(p.name)
            self._move_one_file(p, self.base / bucket)
            touched = True
        if touched:
            self.move_stats.folders_touched += 1

    def _run_recursive_in_place(self) -> None:
        touched_dirs: Set[Path] = set()

        for root, dirs, files in os.walk(self.base, topdown=True):
            root_path = Path(root)

            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
                files = [f for f in files if self._visible_name(f)]

            # Skip traversing known bucket directories.
            dirs[:] = [d for d in dirs if d.upper() not in self.bucket_names]

            if not files:
                continue

            for fn in files:
                src = root_path / fn
                bucket = self._bucket_for_file(fn)
                self._move_one_file(src, root_path / bucket)
                touched_dirs.add(root_path)

        self.move_stats.folders_touched = len(touched_dirs)

    def _run_recursive_flatten_root(self) -> None:
        touched_dirs: Set[Path] = set()

        for root, dirs, files in os.walk(self.base, topdown=True):
            root_path = Path(root)

            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
                files = [f for f in files if self._visible_name(f)]

            # Skip bucket trees to avoid reprocessing newly-created organization folders.
            dirs[:] = [d for d in dirs if d.upper() not in self.bucket_names]

            if not files:
                continue

            for fn in files:
                src = root_path / fn
                bucket = self._bucket_for_file(fn)
                self._move_one_file(src, self.base / bucket)
                touched_dirs.add(root_path)

        self.move_stats.folders_touched = len(touched_dirs)

    def _maybe_normalize(self) -> None:
        if self.normalize != "standard":
            return

        for root, _, _ in os.walk(self.base, topdown=False):
            parent = Path(root)

            # Query live state each iteration (safer if earlier operations renamed dirs).
            for child in [p for p in parent.iterdir() if p.is_dir()]:
                if not self._visible_name(child.name):
                    continue

                canonical = self._canonical_folder_name(child.name)
                if canonical is None or canonical == child.name:
                    continue

                dst = parent / canonical

                # Case-only rename on case-insensitive filesystems:
                # perform via temp path to avoid accidental self-merge.
                same_casefold = child.name.lower() == canonical.lower()
                same_inode = False
                if dst.exists():
                    try:
                        same_inode = os.path.samefile(child, dst)
                    except Exception:
                        same_inode = False

                if same_casefold or same_inode:
                    self.normalize_stats.folders_case_renamed += 1
                    if not self.dry_run:
                        tmp = parent / f"__tmp_norm_{uuid.uuid4().hex[:8]}__"
                        child.rename(tmp)
                        tmp.rename(dst)
                    continue

                if not dst.exists():
                    self.normalize_stats.folders_case_renamed += 1
                    if not self.dry_run:
                        child.rename(dst)
                    continue

                # True merge into existing canonical folder.
                self.normalize_stats.folders_merged += 1
                self._init_reserved_dir(dst)
                reserved = self.reserved_names[dst]

                for item in list(child.iterdir()):
                    if not self._visible_name(item.name):
                        continue

                    target_name = item.name
                    if target_name in reserved:
                        self.normalize_stats.merge_collisions_resolved += 1
                        p = Path(target_name)
                        stem, suffix = p.stem, p.suffix
                        i = 1
                        while True:
                            candidate = f"{stem}_{i}{suffix}" if suffix else f"{target_name}_{i}"
                            if candidate not in reserved:
                                target_name = candidate
                                break
                            i += 1

                    reserved.add(target_name)
                    self.normalize_stats.items_moved_in_merges += 1
                    if not self.dry_run:
                        shutil.move(str(item), str(dst / target_name))

                if not self.dry_run:
                    try:
                        if not any(child.iterdir()):
                            child.rmdir()
                            self.normalize_stats.source_folders_removed += 1
                    except Exception:
                        pass

    def _verify(self) -> Dict[str, object]:
        root_visible = 0
        root_all = 0
        for p in self.base.iterdir():
            if p.is_file():
                root_all += 1
                if self._visible_name(p.name):
                    root_visible += 1

        noncanonical_dirs = []
        for root, dirs, _ in os.walk(self.base, topdown=True):
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
            parent = Path(root)
            for d in dirs:
                c = self._canonical_folder_name(d)
                if c is not None and c != d:
                    noncanonical_dirs.append(str(parent / d))

        remaining_unorganized_visible_files = None
        checked_non_bucket_directories = None

        if self.recursive and self.strategy == "in-place":
            remaining_unorganized_visible_files = 0
            checked_non_bucket_directories = 0

            for root, dirs, files in os.walk(self.base, topdown=True):
                if not self.include_hidden:
                    dirs[:] = [d for d in dirs if self._visible_name(d)]
                    files = [f for f in files if self._visible_name(f)]

                # Direct files in non-bucket directories should be zero after organization.
                checked_non_bucket_directories += 1
                remaining_unorganized_visible_files += len(files)

                # Don't descend into bucket folders for verification of non-bucket dirs.
                dirs[:] = [d for d in dirs if d.upper() not in self.bucket_names]

        return {
            "root_files_remaining_visible": root_visible,
            "root_files_remaining_all": root_all,
            "noncanonical_extension_dirs_count": len(noncanonical_dirs),
            "noncanonical_extension_dirs_sample": noncanonical_dirs[:10],
            "remaining_unorganized_visible_files_in_checked_dirs": remaining_unorganized_visible_files,
            "checked_non_bucket_directories": checked_non_bucket_directories,
        }

    def run(self) -> Dict[str, object]:
        exts = self._collect_extensions()
        self.bucket_names = set(exts)
        self.bucket_names.update({"NO_EXTENSION"})
        self.bucket_names.update(self.alias_map.keys())
        self.bucket_names.update(self.alias_map.values())

        if self.recursive:
            if self.strategy == "in-place":
                self._run_recursive_in_place()
            else:
                self._run_recursive_flatten_root()
        else:
            self._run_non_recursive()

        self._maybe_normalize()

        summary = {
            "target": str(self.base),
            "mode": "recursive" if self.recursive else "non-recursive",
            "strategy": self.strategy if self.recursive else "root-only",
            "include_hidden": self.include_hidden,
            "normalization_mode": self.normalize,
            "dry_run": self.dry_run,
            "files_moved": self.move_stats.files_moved,
            "moved_by_extension": dict(sorted(self.ext_counts.items())),
            "name_collisions_resolved": self.move_stats.name_collisions_resolved,
            "folders_touched": self.move_stats.folders_touched,
            "normalization": {
                "folders_case_renamed": self.normalize_stats.folders_case_renamed,
                "folders_merged": self.normalize_stats.folders_merged,
                "items_moved_in_merges": self.normalize_stats.items_moved_in_merges,
                "merge_collisions_resolved": self.normalize_stats.merge_collisions_resolved,
                "source_folders_removed": self.normalize_stats.source_folders_removed,
                "alias_map": self.alias_map,
            },
            "verification": self._verify(),
        }
        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize files by extension folders with optional recursive mode and normalization.")
    parser.add_argument("--path", required=True, help="Target directory path")
    parser.add_argument("--recursive", action="store_true", help="Enable recursive organization")
    parser.add_argument(
        "--strategy",
        choices=["in-place", "flatten-root"],
        default="in-place",
        help="Recursive strategy (ignored when not recursive)",
    )
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden files/folders")
    parser.add_argument(
        "--normalize",
        choices=["none", "standard"],
        default="none",
        help="Normalization mode (standard applies alias merge + uppercase bucket casing)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate operations without writing changes")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    base = Path(args.path).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        print(json.dumps({"error": f"Path not found or not a directory: {base}"}, indent=2))
        return

    normalize = args.normalize
    if args.recursive and normalize == "none":
        # Keep explicit 'none' when chosen by caller; skill layer can override defaults.
        pass

    org = Organizer(
        base=base,
        recursive=args.recursive,
        strategy=args.strategy,
        include_hidden=args.include_hidden,
        normalize=normalize,
        dry_run=args.dry_run,
    )
    result = org.run()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
