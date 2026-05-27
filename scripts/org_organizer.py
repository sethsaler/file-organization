#!/usr/bin/env python3
"""Core organizer implementation."""
from __future__ import annotations

import csv
import json
import os
import random
import re
import shutil
import string
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from org_buckets import bucket_for_extension, bucket_names_for_profile, canonical_dir_map
from org_exclude import path_excluded, should_skip_traverse_dir
from org_manifest import (
    FOR_DELETION_DIR_NAME,
    Manifest,
    ManifestEntry,
    ORGANIZER_DIR_NAME,
    write_manifest_files,
)
from org_mime import sniff_bucket_from_file

EMPTY_DIR_SAMPLE_LIMIT = 20

# Pattern to detect randomly renamed files: 16 alphanumeric characters before extension
RANDOM_NAME_PATTERN = re.compile(r'^[A-Za-z0-9]{16}\.[^.]+$')

# MIME sniff can classify extensionless files into buckets that only exist on the extended profile;
# allow these when using the standard profile so sniffing is not a silent no-op.
_MIME_SNIFF_STANDARD_EXTRA_BUCKETS = frozenset({"Documents", "Archives", "Audio"})


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
        create_backup: bool = True,
        *,
        profile_label: str = "standard",
        profile_buckets: Optional[List[Tuple[str, Set[str]]]] = None,
        exclude_patterns: Optional[List[str]] = None,
        follow_symlinks: bool = True,
        use_mime_sniff: bool = False,
        verbose: bool = False,
        ocr_index: bool = False,
        progress_callback: Optional[Callable[[str], None]] = None,
        random_names: bool = False,
        random_names_after_organize: bool = False,
        skip_randomly_renamed: bool = False,
    ) -> None:
        self.base = base
        self.recursive = recursive
        self.strategy = strategy
        self.include_hidden = include_hidden
        self.normalize = normalize
        self.collect_empty_dirs = collect_empty_dirs
        self.dry_run = dry_run
        self.create_backup = create_backup
        self.profile_label = profile_label
        self.profile_buckets = profile_buckets or []
        if profile_label.startswith("custom:") and not self.profile_buckets:
            raise ValueError("Custom profile requires at least one bucket definition")
        self.exclude_patterns = list(exclude_patterns or [])
        self.follow_symlinks = follow_symlinks
        self.use_mime_sniff = use_mime_sniff
        self.verbose = verbose
        self.ocr_index = ocr_index
        self.random_names = random_names
        self.random_names_after_organize = random_names_after_organize
        self.skip_randomly_renamed = skip_randomly_renamed
        self._progress = progress_callback or (lambda _m: None)
        prof_key = profile_label if profile_label in ("standard", "extended") else "standard"
        if profile_buckets and len(profile_buckets) > 3:
            prof_key = "extended"
        self._category_canonical = canonical_dir_map(prof_key)
        if profile_buckets and profile_label.startswith("custom:"):
            self._category_canonical = {n.casefold(): n for n, _ in profile_buckets}
            self._category_canonical["other"] = "Other"

        self.ext_counts = Counter()
        self.move_stats = MoveStats()
        self.normalize_stats = NormalizeStats()
        self.empty_dir_stats = EmptyDirStats()
        self.empty_dirs_removed = 0
        self.file_moves: List[ManifestEntry] = []
        self.empty_dir_moves: List[ManifestEntry] = []
        self.removed_dirs: List[str] = []

        self.reserved_names: Dict[Path, Set[str]] = defaultdict(set)
        self.used_random_names: Set[str] = set()

    def _visible_name(self, name: str) -> bool:
        return self.include_hidden or not name.startswith(".")

    def _is_for_deletion_name(self, name: str) -> bool:
        return name.casefold() == FOR_DELETION_DIR_NAME.casefold()

    def _is_organizer_dir(self, name: str) -> bool:
        return name.casefold() == ORGANIZER_DIR_NAME.casefold()

    def _at_organize_root(self, parent_path: Path) -> bool:
        try:
            return parent_path.resolve() == self.base
        except OSError:
            return False

    def _should_skip_traversal_dir(self, parent_path: Path, dir_name: str) -> bool:
        if self._is_for_deletion_name(dir_name):
            return True
        if self._is_organizer_dir(dir_name):
            return True
        if should_skip_traverse_dir(parent_path, dir_name, self.base, self.exclude_patterns):
            return True
        return False

    def _walk_topdown_organize(self) -> Iterator[Tuple[Path, List[str], List[str]]]:
        """Walk the tree for file moves: follow symlinks into directories, break symlink cycles via inode."""
        visited: Set[Tuple[int, int]] = set()
        for root, dirs, files in os.walk(self.base, topdown=True, followlinks=self.follow_symlinks):
            root_path = Path(root)
            try:
                st = os.stat(root_path, follow_symlinks=False)
                key = (st.st_dev, st.st_ino)
                if key in visited:
                    dirs[:] = []
                    continue
                visited.add(key)
            except OSError:
                pass
            yield root_path, dirs, files

    def _purge_hidden_files_for_cleanup(self, directory: Path) -> None:
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

    def _canonical_folder_name(self, name: str) -> Optional[str]:
        canon = self._category_canonical.get(name.casefold())
        if canon is not None and canon != name:
            return canon
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
            if name not in self.used_random_names:
                self.used_random_names.add(name)
                return name

    def _collision_safe_target(self, dest_dir: Path, original_name: str, collision_counter: str = "files") -> Path:
        self._init_reserved_dir(dest_dir)
        reserved = self.reserved_names[dest_dir]

        # If random_names is enabled, generate a random name with the original extension
        if self.random_names:
            extension = Path(original_name).suffix
            random_name = self._generate_random_name(extension)
            reserved.add(random_name)
            return dest_dir / random_name

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

    def _allowed_bucket_names(self) -> Set[str]:
        if self.profile_buckets:
            return {name for name, _ in self.profile_buckets} | {"Other"}
        prof = self.profile_label if self.profile_label in ("standard", "extended") else "standard"
        return set(bucket_names_for_profile(prof))

    def _bucket_for_file(self, file_name: str, src: Optional[Path] = None) -> str:
        suffix = Path(file_name).suffix
        ext = suffix[1:].upper() if suffix else ""
        prof = self.profile_label if self.profile_label in ("standard", "extended") else "standard"
        if self.profile_buckets:
            bucket = bucket_for_extension(ext, prof, self.profile_buckets)
        else:
            bucket = bucket_for_extension(ext, prof)
        if bucket == "Other" and not ext and self.use_mime_sniff and src is not None:
            sniffed = sniff_bucket_from_file(src)
            if sniffed:
                allowed = self._allowed_bucket_names()
                if sniffed in allowed or (
                    not self.profile_buckets
                    and self.profile_label == "standard"
                    and sniffed in _MIME_SNIFF_STANDARD_EXTRA_BUCKETS
                ):
                    return sniffed
        return bucket

    def _move_one_file(self, src: Path, dest_dir: Path) -> None:
        try:
            if src.resolve().parent.resolve() == dest_dir.resolve():
                return
        except OSError:
            pass

        dest_dir_name = dest_dir.name
        self.ext_counts[dest_dir_name] += 1

        if not self.dry_run and not dest_dir.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
        elif self.dry_run:
            self._init_reserved_dir(dest_dir)

        target = self._collision_safe_target(dest_dir, src.name)

        if not self.dry_run:
            shutil.move(str(src), str(target))
            rel_src = str(src.relative_to(self.base))
            rel_dst = str(target.relative_to(self.base))
            self.file_moves.append(ManifestEntry(from_path=rel_src, to_path=rel_dst))

        self.move_stats.files_moved += 1
        if self.verbose and self.move_stats.files_moved % 100 == 0:
            self._progress(f"Moved {self.move_stats.files_moved} files…\n")

    def _run_non_recursive(self) -> None:
        touched = False
        for p in list(self.base.iterdir()):
            if not p.is_file() or not self._visible_name(p.name):
                continue
            bucket = self._bucket_for_file(p.name, p)
            self._move_one_file(p, self.base / bucket)
            touched = True
        if touched:
            self.move_stats.folders_touched += 1

    def _run_recursive_in_place(self) -> None:
        touched_dirs: Set[Path] = set()

        for root_path, dirs, files in self._walk_topdown_organize():
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
                files = [f for f in files if self._visible_name(f)]

            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]

            if not files:
                continue

            for fn in files:
                src = root_path / fn
                bucket = self._bucket_for_file(fn, src)
                self._move_one_file(src, root_path / bucket)
                touched_dirs.add(root_path)

        self.move_stats.folders_touched = len(touched_dirs)

    def _run_recursive_flatten_root(self) -> None:
        touched_dirs: Set[Path] = set()

        for root_path, dirs, files in self._walk_topdown_organize():
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
                files = [f for f in files if self._visible_name(f)]

            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]

            if not files:
                continue

            for fn in files:
                src = root_path / fn
                bucket = self._bucket_for_file(fn, src)
                self._move_one_file(src, self.base / bucket)
                touched_dirs.add(root_path)

        self.move_stats.folders_touched = len(touched_dirs)

    def _maybe_normalize(self) -> None:
        if self.normalize != "standard":
            return

        # Bottom-up pass with the same directory pruning as organize (excludes, hidden, reserved dirs).
        roots: List[Path] = []
        for root, dirs, _ in os.walk(self.base, topdown=True, followlinks=False):
            root_path = Path(root)
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
            if self._is_for_deletion_name(root_path.name):
                dirs[:] = []
                continue
            if self._is_organizer_dir(root_path.name):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]
            roots.append(root_path)

        for parent in reversed(roots):
            if self._is_for_deletion_name(parent.name):
                continue
            if self._is_organizer_dir(parent.name):
                continue

            for child in [p for p in parent.iterdir() if p.is_dir()]:
                if self._is_for_deletion_name(child.name):
                    continue
                if self._is_organizer_dir(child.name):
                    continue
                if not self._visible_name(child.name):
                    continue

                canonical = self._canonical_folder_name(child.name)
                if canonical is None or canonical == child.name:
                    continue

                dst = parent / canonical

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
                        rel_from = str(child.relative_to(self.base))
                        tmp = parent / f"__tmp_norm_{uuid.uuid4().hex[:8]}__"
                        child.rename(tmp)
                        tmp.rename(dst)
                        self.file_moves.append(ManifestEntry(from_path=rel_from, to_path=str(dst.relative_to(self.base))))
                    continue

                if not dst.exists():
                    self.normalize_stats.folders_case_renamed += 1
                    if not self.dry_run:
                        rel_from = str(child.relative_to(self.base))
                        child.rename(dst)
                        self.file_moves.append(ManifestEntry(from_path=rel_from, to_path=str(dst.relative_to(self.base))))
                    continue

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
                        dest_item = dst / target_name
                        shutil.move(str(item), str(dest_item))
                        self.file_moves.append(
                            ManifestEntry(
                                from_path=str(item.relative_to(self.base)),
                                to_path=str(dest_item.relative_to(self.base)),
                            )
                        )

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
                    profile_label=self.profile_label,
                    profile_buckets=list(self.profile_buckets) if self.profile_buckets else None,
                    exclude_patterns=list(self.exclude_patterns),
                    follow_symlinks=self.follow_symlinks,
                    use_mime_sniff=self.use_mime_sniff,
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
        if path_excluded(directory, self.base, self.exclude_patterns):
            return False, []
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
            if self._is_organizer_dir(entry.name):
                collectable = False
                continue
            if entry.is_dir() and self._should_skip_traversal_dir(directory, entry.name):
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
            if self._is_organizer_dir(child.name):
                continue
            if self._should_skip_traversal_dir(self.base, child.name):
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

    def _collect_empty_dirs_batch(self, candidates: List[Path]) -> None:
        """Move one batch of empty-folder candidates into root-level For Deletion."""
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
                rel_src = str(src_dir.relative_to(self.base))
                rel_dst = str(target.relative_to(self.base))
                self.empty_dir_moves.append(ManifestEntry(from_path=rel_src, to_path=rel_dst))

            self.empty_dir_stats.folders_moved += 1

    def _maybe_collect_empty_dirs(self) -> None:
        if not self.collect_empty_dirs:
            return

        if self.dry_run:
            simulated = self._simulate_empty_dir_collection()
            if simulated is not None:
                self.empty_dir_stats = simulated
                return

        max_rounds = 500
        for _ in range(max_rounds):
            candidates = self._find_empty_dir_candidates()
            if not candidates:
                break
            self._collect_empty_dirs_batch(candidates)

    def _remove_empty_subdirs(self) -> None:
        candidates: List[Path] = []
        for root, dirs, files in os.walk(self.base, topdown=True, followlinks=False):
            root_path = Path(root)
            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]
            if self._is_for_deletion_name(root_path.name):
                dirs[:] = []
                continue
            if self._is_organizer_dir(root_path.name):
                dirs[:] = []
                continue
            if self._at_organize_root(root_path):
                continue
            candidates.append(root_path)

        for root_path in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
            if self._is_for_deletion_name(root_path.name):
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
                    self.removed_dirs.append(str(root_path.relative_to(self.base)))
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
        for root_path, dirs, _ in self._walk_topdown_organize():
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
            dirs[:] = [d for d in dirs if not self._is_for_deletion_name(d)]
            dirs[:] = [d for d in dirs if not self._is_organizer_dir(d)]
            parent = root_path
            for d in dirs:
                c = self._canonical_folder_name(d)
                if c is not None and c != d:
                    noncanonical_dirs.append(str(parent / d))

        remaining_unorganized_visible_files = None
        checked_non_bucket_directories = None

        if self.recursive and self.strategy == "in-place":
            remaining_unorganized_visible_files = 0
            checked_non_bucket_directories = 0

            for root_path, dirs, files in self._walk_topdown_organize():
                if not self.include_hidden:
                    dirs[:] = [d for d in dirs if self._visible_name(d)]
                    files = [f for f in files if self._visible_name(f)]

                checked_non_bucket_directories += 1
                remaining_unorganized_visible_files += len(files)

                dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]

        return {
            "root_files_remaining_visible": root_visible,
            "root_files_remaining_all": root_all,
            "noncanonical_bucket_dirs_count": len(noncanonical_dirs),
            "noncanonical_bucket_dirs_sample": noncanonical_dirs[:10],
            "remaining_unorganized_visible_files_in_checked_dirs": remaining_unorganized_visible_files,
            "checked_non_bucket_directories": checked_non_bucket_directories,
        }

    def save_manifest(self) -> Optional[Dict[str, str]]:
        manifest = Manifest(
            created_at=datetime.now().isoformat(),
            base_path=str(self.base),
            mode="recursive" if self.recursive else "non-recursive",
            strategy=self.strategy if self.recursive else "root-only",
            normalize=self.normalize,
            profile=self.profile_label,
            file_moves=self.file_moves,
            empty_dir_moves=self.empty_dir_moves,
            empty_dirs_removed=self.removed_dirs,
        )
        helper = Path(__file__).resolve().parent / "organize_by_filetype.py"
        return write_manifest_files(
            self.base,
            manifest,
            helper_script=helper,
            dry_run=self.dry_run,
            create_backup=self.create_backup,
        )


    def _maybe_write_ocr_index(self) -> Optional[str]:
        if not self.ocr_index or self.dry_run:
            return None
        images_dir = self.base / "Images"
        if not images_dir.is_dir():
            return None
        out_path = self.base / ORGANIZER_DIR_NAME / "ocr_index.csv"
        if not self.dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for img in sorted(images_dir.rglob("*")):
            if not img.is_file():
                continue
            if img.suffix.upper() not in {".PNG", ".JPG", ".JPEG"}:
                continue
            text_val = ""
            err = None
            try:
                from PIL import Image
                import pytesseract
                with Image.open(img) as im:
                    text_val = (pytesseract.image_to_string(im, lang="eng") or "").strip()
            except Exception as e:
                err = str(e)
            rows.append((str(img.relative_to(self.base)), text_val, err or ""))
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["file_name", "extracted_text", "error"])
            w.writerows(rows)
        return str(out_path)

    def _rename_all_files_after_organize(self) -> Dict[str, object]:
        """Rename all files with random names after organization is complete."""
        rename_stats = {"files_renamed": 0, "files_skipped": 0, "errors": []}
        
        # Collect all files from the organized structure
        all_files = []
        for root, dirs, files in os.walk(self.base, topdown=False):
            root_path = Path(root)
            
            # Skip organizer and deletion directories
            if self._is_organizer_dir(root_path.name) or self._is_for_deletion_name(root_path.name):
                dirs[:] = []
                continue
                
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
                files = [f for f in files if self._visible_name(f)]
                
            for filename in files:
                src = root_path / filename
                if src.is_file() and not src.is_symlink():
                    all_files.append(src)
        
        # Rename each file
        for src in all_files:
            try:
                # Skip files that appear to be already randomly renamed if flag is enabled
                if self.skip_randomly_renamed and self._is_randomly_renamed(src.name):
                    rename_stats["files_skipped"] += 1
                    continue
                
                new_name = self._generate_random_name(src.suffix)
                dest = src.parent / new_name
                
                if dest.exists():
                    rename_stats["errors"].append(f"Destination already exists: {dest}")
                    continue
                
                if not self.dry_run:
                    src.rename(dest)
                    # Update the file_moves manifest with the new name
                    rel_src = str(src.relative_to(self.base))
                    rel_dst = str(dest.relative_to(self.base))
                    self.file_moves.append(ManifestEntry(from_path=rel_src, to_path=rel_dst))
                
                rename_stats["files_renamed"] += 1
                
                if self.verbose and rename_stats["files_renamed"] % 100 == 0:
                    self._progress(f"Renamed {rename_stats['files_renamed']} files after organization…\n")
                    
            except Exception as e:
                rename_stats["errors"].append(f"Error renaming {src}: {e}")
        
        return rename_stats

    def run(self) -> Dict[str, object]:
        try:
            self.base = self.base.resolve()
        except OSError:
            pass

        if self.recursive:
            if self.strategy == "in-place":
                self._run_recursive_in_place()
            else:
                self._run_recursive_flatten_root()
                if not self.collect_empty_dirs:
                    self._remove_empty_subdirs()
        else:
            self._run_non_recursive()

        self._maybe_normalize()
        self._maybe_collect_empty_dirs()
        self._remove_empty_subdirs()

        # Rename all files after organization if requested
        rename_stats = {}
        if self.random_names_after_organize:
            rename_stats = self._rename_all_files_after_organize()

        manifest_info = self.save_manifest()
        ocr_path = self._maybe_write_ocr_index()

        if self.profile_buckets:
            summary_buckets: List[str] = [name for name, _ in self.profile_buckets] + ["Other"]
        else:
            prof = self.profile_label if self.profile_label in ("standard", "extended") else "standard"
            summary_buckets = bucket_names_for_profile(prof)

        summary = {
            "target": str(self.base),
            "mode": "recursive" if self.recursive else "non-recursive",
            "strategy": self.strategy if self.recursive else "root-only",
            "buckets": summary_buckets,
            "profile": self.profile_label,
            "include_hidden": self.include_hidden,
            "normalization_mode": self.normalize,
            "dry_run": self.dry_run,
            "files_moved": self.move_stats.files_moved,
            "moved_by_category": dict(sorted(self.ext_counts.items())),
            "name_collisions_resolved": self.move_stats.name_collisions_resolved,
            "folders_touched": self.move_stats.folders_touched,
            "empty_dirs_removed": self.empty_dirs_removed,
            "normalization": {
                "folders_case_renamed": self.normalize_stats.folders_case_renamed,
                "folders_merged": self.normalize_stats.folders_merged,
                "items_moved_in_merges": self.normalize_stats.items_moved_in_merges,
                "merge_collisions_resolved": self.normalize_stats.merge_collisions_resolved,
                "source_folders_removed": self.normalize_stats.source_folders_removed,
            },
            "empty_folder_collection": {
                "enabled": self.collect_empty_dirs,
                "destination": str(self.base / FOR_DELETION_DIR_NAME) if self.collect_empty_dirs else None,
                "folders_moved": self.empty_dir_stats.folders_moved,
                "name_collisions_resolved": self.empty_dir_stats.name_collisions_resolved,
                "sample_moves": self.empty_dir_stats.sample_moves,
            },
            "rename_after_organize": {
                "enabled": self.random_names_after_organize,
                "files_renamed": rename_stats.get("files_renamed", 0),
                "files_skipped": rename_stats.get("files_skipped", 0),
                "errors": rename_stats.get("errors", []),
            },
            "verification": self._verify(),
            "backup_manifest": manifest_info.get("manifest") if manifest_info else None,
            "restore_script": manifest_info.get("restore_script") if manifest_info else None,
            "restore_sh": manifest_info.get("restore_sh") if manifest_info else None,
            "restore_cli": manifest_info.get("restore_cli") if manifest_info else None,
            "ocr_index": ocr_path,
        }
        return summary
