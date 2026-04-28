#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set


ALIAS_MAP_DEFAULT: Dict[str, str] = {
    "JPEG": "JPG",
    "JPE": "JPG",
}

FOR_DELETION_DIR_NAME = "For Deletion"
EMPTY_DIR_SAMPLE_LIMIT = 20


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


@dataclass
class EmptyDirStats:
    folders_moved: int = 0
    name_collisions_resolved: int = 0
    sample_moves: List[Dict[str, str]] = field(default_factory=list)


class Organizer:
    def __init__(
        self,
        base: Path,
        recursive: bool,
        strategy: str,
        include_hidden: bool,
        normalize: str,
        collect_empty_dirs: bool,
        dry_run: bool,
    ) -> None:
        self.base = base
        self.recursive = recursive
        self.strategy = strategy
        self.include_hidden = include_hidden
        self.normalize = normalize
        self.collect_empty_dirs = collect_empty_dirs
        self.dry_run = dry_run

        self.alias_map = dict(ALIAS_MAP_DEFAULT)
        self.ext_counts = Counter()
        self.move_stats = MoveStats()
        self.normalize_stats = NormalizeStats()
        self.empty_dir_stats = EmptyDirStats()
        self.empty_dirs_removed = 0

        # Destination reservation map used for deterministic collision-safe naming.
        # Helps keep behavior predictable, especially in dry-run mode.
        self.reserved_names: Dict[Path, Set[str]] = defaultdict(set)

        self.bucket_names: Set[str] = set()

    def _visible_name(self, name: str) -> bool:
        return self.include_hidden or not name.startswith(".")

    def _is_for_deletion_name(self, name: str) -> bool:
        return name.casefold() == FOR_DELETION_DIR_NAME.casefold()

    def _should_skip_traversal_dir(self, parent_path: Path, dir_name: str) -> bool:
        """Skip descending only into organizer output dirs directly under base.

        Nested folders whose names match an extension (e.g. photos/JPG/) must still
        be walked so files inside are flattened into root buckets.
        """
        if parent_path != self.base:
            return False
        if self._is_for_deletion_name(dir_name):
            return True
        return dir_name.upper() in self.bucket_names

    def _is_root_level_bucket_dir(self, path: Path) -> bool:
        """Extension bucket folder placed directly under base — never removed when pruning empties."""
        return path.parent == self.base and path.name.upper() in self.bucket_names

    def _purge_hidden_files_for_cleanup(self, directory: Path) -> None:
        """Remove ignored dotfiles (e.g. .DS_Store) so shells empty after organize can be deleted."""
        if self.include_hidden:
            return
        try:
            for entry in list(directory.iterdir()):
                if entry.is_file() and not self._visible_name(entry.name):
                    if not self.dry_run:
                        try:
                            entry.unlink()
                        except OSError:
                            pass
        except OSError:
            pass

    def _note_collision(self, collision_counter: str) -> None:
        if collision_counter == "files":
            self.move_stats.name_collisions_resolved += 1
        else:
            self.empty_dir_stats.name_collisions_resolved += 1

    def _collect_extensions(self) -> Set[str]:
        exts: Set[str] = set()
        for root, dirs, files in os.walk(self.base, topdown=True):
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
            dirs[:] = [d for d in dirs if not self._is_for_deletion_name(d)]
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

    def _collision_safe_target(self, dest_dir: Path, original_name: str, collision_counter: str = "files") -> Path:
        self._init_reserved_dir(dest_dir)
        reserved = self.reserved_names[dest_dir]

        if original_name not in reserved:
            reserved.add(original_name)
            return dest_dir / original_name

        self._note_collision(collision_counter)
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

            # Skip traversing root-level bucket dirs only (see _should_skip_traversal_dir).
            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]

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

            # Skip traversing root-level bucket dirs only (nested JPG/PDF/… folders still scanned).
            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]

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
            if self._is_for_deletion_name(parent.name):
                continue

            # Query live state each iteration (safer if earlier operations renamed dirs).
            for child in [p for p in parent.iterdir() if p.is_dir()]:
                if self._is_for_deletion_name(child.name):
                    continue
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

    def _copy_placeholder_tree(self, src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            target = dst / entry.name
            if entry.is_symlink():
                try:
                    os.symlink(os.readlink(entry), target)
                except Exception:
                    target.touch()
                continue

            if entry.is_dir():
                self._copy_placeholder_tree(entry, target)
                continue

            target.touch()

    def _simulate_empty_dir_collection(self) -> Optional[EmptyDirStats]:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                sim_base = Path(tmpdir) / "sim"
                self._copy_placeholder_tree(self.base, sim_base)
                sim_org = Organizer(
                    base=sim_base,
                    recursive=self.recursive,
                    strategy=self.strategy,
                    include_hidden=self.include_hidden,
                    normalize=self.normalize,
                    collect_empty_dirs=True,
                    dry_run=False,
                )
                sim_org.run()
                return EmptyDirStats(
                    folders_moved=sim_org.empty_dir_stats.folders_moved,
                    name_collisions_resolved=sim_org.empty_dir_stats.name_collisions_resolved,
                    sample_moves=list(sim_org.empty_dir_stats.sample_moves),
                )
        except Exception:
            return None

    def _inspect_empty_dir_tree(self, directory: Path) -> tuple[bool, List[Path]]:
        if directory.is_symlink():
            return False, []

        try:
            entries = list(directory.iterdir())
        except Exception:
            return False, []

        if not entries:
            return True, []

        collectable = True
        topmost_children: List[Path] = []

        for entry in entries:
            if self._is_for_deletion_name(entry.name):
                collectable = False
                continue

            if not self.include_hidden and not self._visible_name(entry.name):
                collectable = False
                continue

            if entry.is_symlink():
                collectable = False
                continue

            if entry.is_dir():
                child_collectable, child_topmost = self._inspect_empty_dir_tree(entry)
                if child_collectable:
                    topmost_children.append(entry)
                else:
                    collectable = False
                    topmost_children.extend(child_topmost)
                continue

            collectable = False

        if collectable:
            return True, []
        return False, topmost_children

    def _find_empty_dir_candidates(self) -> List[Path]:
        candidates: List[Path] = []
        seen: Set[Path] = set()

        for child in sorted(self.base.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.is_symlink():
                continue
            if not self._visible_name(child.name):
                continue
            if self._is_for_deletion_name(child.name):
                continue

            child_collectable, child_topmost = self._inspect_empty_dir_tree(child)
            if child_collectable:
                paths_to_add = [child]
            elif self.recursive:
                paths_to_add = child_topmost
            else:
                paths_to_add = []

            for path in paths_to_add:
                if path in seen:
                    continue
                seen.add(path)
                candidates.append(path)

        return candidates

    def _maybe_collect_empty_dirs(self) -> None:
        if not self.collect_empty_dirs:
            return

        if self.dry_run:
            simulated = self._simulate_empty_dir_collection()
            if simulated is not None:
                self.empty_dir_stats = simulated
                return

        candidates = self._find_empty_dir_candidates()
        if not candidates:
            return

        dest_root = self.base / FOR_DELETION_DIR_NAME
        if not self.dry_run:
            dest_root.mkdir(parents=True, exist_ok=True)
        else:
            self._init_reserved_dir(dest_root)

        for src_dir in candidates:
            target = self._collision_safe_target(dest_root, src_dir.name, collision_counter="empty_dirs")
            if len(self.empty_dir_stats.sample_moves) < EMPTY_DIR_SAMPLE_LIMIT:
                self.empty_dir_stats.sample_moves.append(
                    {
                        "from": str(src_dir.relative_to(self.base)),
                        "to": str(target.relative_to(self.base)),
                    }
                )

            if not self.dry_run:
                shutil.move(str(src_dir), str(target))

            self.empty_dir_stats.folders_moved += 1

    def _remove_empty_subdirs(self) -> None:
        """Remove leftover folders under base after flatten-root (deepest first).

        Skips descending into root-level extension buckets so large JPG/MP4 trees are not scanned.
        Deletes ignored hidden files first — otherwise folders that only contain `.DS_Store` would
        never count as empty.
        """
        candidates: List[Path] = []
        for root, dirs, files in os.walk(self.base, topdown=True):
            root_path = Path(root)
            # Exclude root-level bucket / For Deletion trees from descent (large JPG/MP4 dirs).
            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]
            if self._is_for_deletion_name(root_path.name):
                dirs[:] = []
                continue
            if root_path == self.base:
                continue
            if self._is_root_level_bucket_dir(root_path):
                continue
            candidates.append(root_path)

        for root_path in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
            if self._is_for_deletion_name(root_path.name):
                continue
            if self._is_root_level_bucket_dir(root_path):
                continue
            try:
                entries = list(root_path.iterdir())
            except OSError:
                continue

            can_remove = False
            if not entries:
                can_remove = True
            elif not self.include_hidden:
                can_remove = all(e.is_file() and not self._visible_name(e.name) for e in entries)

            if not can_remove:
                continue

            if not self.dry_run:
                self._purge_hidden_files_for_cleanup(root_path)
                try:
                    if any(root_path.iterdir()):
                        continue
                    root_path.rmdir()
                except OSError:
                    continue
            self.empty_dirs_removed += 1

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
            dirs[:] = [d for d in dirs if not self._is_for_deletion_name(d)]
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

                # Don't descend into root-level bucket dirs only.
                dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(Path(root), d)]

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
                self._remove_empty_subdirs()
        else:
            self._run_non_recursive()

        self._maybe_normalize()
        # Normalization can empty folders that were not removed in the first pass.
        if self.recursive and self.strategy == "flatten-root" and not self.collect_empty_dirs:
            self._remove_empty_subdirs()
        self._maybe_collect_empty_dirs()

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
            "empty_dirs_removed": self.empty_dirs_removed,
            "normalization": {
                "folders_case_renamed": self.normalize_stats.folders_case_renamed,
                "folders_merged": self.normalize_stats.folders_merged,
                "items_moved_in_merges": self.normalize_stats.items_moved_in_merges,
                "merge_collisions_resolved": self.normalize_stats.merge_collisions_resolved,
                "source_folders_removed": self.normalize_stats.source_folders_removed,
                "alias_map": self.alias_map,
            },
            "empty_folder_collection": {
                "enabled": self.collect_empty_dirs,
                "destination": str(self.base / FOR_DELETION_DIR_NAME) if self.collect_empty_dirs else None,
                "folders_moved": self.empty_dir_stats.folders_moved,
                "name_collisions_resolved": self.empty_dir_stats.name_collisions_resolved,
                "sample_moves": self.empty_dir_stats.sample_moves,
            },
            "verification": self._verify(),
        }
        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize files by extension folders with optional recursive mode and normalization.")
    parser.add_argument("--path", required=True, help="Target directory path")
    parser.add_argument("--recursive", action="store_true", default=True, help="Enable recursive organization (default)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Disable recursive, root files only")
    parser.add_argument(
        "--strategy",
        choices=["in-place", "flatten-root"],
        default="flatten-root",
        help="Recursive strategy (ignored when not recursive)",
    )
    hidden_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(include_hidden=True)
    hidden_group.add_argument(
        "--include-hidden",
        dest="include_hidden",
        action="store_true",
        help="Include hidden files and folders (default)",
    )
    hidden_group.add_argument(
        "--no-include-hidden",
        dest="include_hidden",
        action="store_false",
        help="Exclude dotfiles and dot-directories from organizing and empty-folder cleanup",
    )
    parser.add_argument(
        "--normalize",
        choices=["none", "standard"],
        default="none",
        help="Normalization mode (standard applies alias merge + uppercase bucket casing)",
    )
    empty_dir_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(collect_empty_dirs=True)
    empty_dir_group.add_argument(
        "--collect-empty-dirs",
        dest="collect_empty_dirs",
        action="store_true",
        help="Move collectable empty folders into a root-level 'For Deletion' folder (default)",
    )
    empty_dir_group.add_argument(
        "--no-collect-empty-dirs",
        dest="collect_empty_dirs",
        action="store_false",
        help="Disable automatic empty-folder collection into 'For Deletion'",
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
        collect_empty_dirs=args.collect_empty_dirs,
        dry_run=args.dry_run,
    )
    result = org.run()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
