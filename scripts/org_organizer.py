#!/usr/bin/env python3
"""Core organizer implementation."""
from __future__ import annotations

import csv
import json
import os
import random
import re
import shutil
import stat as stat_module
import string
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from org_buckets import (
    bucket_names_for_profile,
    buckets_for_profile,
    canonical_dir_map,
    extension_bucket_map,
)
from org_dupes import DuplicateIndex, stat_is_dataless
from org_exclude import path_excluded, should_skip_traverse_dir
from org_manifest import (
    FOR_DELETION_DIR_NAME,
    Manifest,
    ManifestEntry,
    ORGANIZER_DIR_NAME,
    cleanup_old_manifests,
    write_manifest_files,
)
from org_mime import sniff_bucket_from_file
from org_rules import (
    NEEDS_REVIEW_DIR_NAME,
    ArchiveRecipe,
    RouteDecision,
    RuleSet,
    build_rule_context,
)

EMPTY_DIR_SAMPLE_LIMIT = 20
DUPLICATES_DIR_NAME = "Duplicates"
DUPLICATE_SAMPLE_LIMIT = 20
PLANNED_MOVE_SAMPLE_LIMIT = 200

# Pattern to detect randomly renamed files: 16 alphanumeric characters before extension
RANDOM_NAME_PATTERN = re.compile(r'^[A-Za-z0-9]{16}\.[^.]+$')

# Extensionless variant: a randomly renamed file that had no extension to preserve
RANDOM_NAME_CANDIDATE_PATTERN = re.compile(r'^[A-Za-z0-9]{16}$')

# MIME sniff can classify extensionless files into buckets that only exist on the extended profile;
# allow these when using the standard profile so sniffing is not a silent no-op.
_MIME_SNIFF_STANDARD_EXTRA_BUCKETS = frozenset({"Documents", "Archives", "Audio"})


class _SimDir:
    """In-memory directory node used to preview empty-folder collection in dry runs."""

    __slots__ = ("path", "parent", "is_symlink", "subdirs", "files")

    def __init__(self, path: Path, parent: Optional["_SimDir"], is_symlink: bool) -> None:
        self.path = path
        self.parent = parent
        self.is_symlink = is_symlink
        self.subdirs: Dict[str, "_SimDir"] = {}
        self.files: Dict[str, bool] = {}


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


@dataclass
class DuplicateStats:
    files_moved: int = 0
    files_hardlinked: int = 0
    sample_moves: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class RoutingStats:
    matched_by_rule: Counter = field(default_factory=Counter)
    needs_review_files: int = 0
    left_in_place: int = 0
    external_moves: int = 0


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
        detect_duplicates: bool = False,
        duplicates_hardlink: bool = False,
        date_buckets: bool = False,
        rule_set: Optional[RuleSet] = None,
        archive_recipe: Optional[ArchiveRecipe] = None,
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
        self.detect_duplicates = detect_duplicates
        self.duplicates_hardlink = duplicates_hardlink
        self.date_buckets = date_buckets
        self.rule_set = rule_set
        self.archive_recipe = archive_recipe
        if self.archive_recipe is not None:
            self.archive_recipe.validate_source_root(base)
        self._progress = progress_callback or (lambda _m: None)
        prof_key = profile_label if profile_label in ("standard", "extended") else "standard"
        if profile_buckets and len(profile_buckets) > 3:
            prof_key = "extended"
        self._category_canonical = canonical_dir_map(prof_key)
        if profile_buckets and profile_label.startswith("custom:"):
            self._category_canonical = {n.casefold(): n for n, _ in profile_buckets}
            self._category_canonical["other"] = "Other"

        # One ext -> bucket dict so per-file classification is O(1) instead of a
        # linear scan over every bucket's extension set.
        prof_for_exts = profile_label if profile_label in ("standard", "extended") else "standard"
        effective_buckets = self.profile_buckets or buckets_for_profile(prof_for_exts)
        self._ext_bucket_map = extension_bucket_map(effective_buckets)

        self.ext_counts = Counter()
        self.move_stats = MoveStats()
        self.normalize_stats = NormalizeStats()
        self.empty_dir_stats = EmptyDirStats()
        self.duplicate_stats = DuplicateStats()
        self.routing_stats = RoutingStats()
        self.empty_dirs_removed = 0
        self.file_moves: List[ManifestEntry] = []
        self.empty_dir_moves: List[ManifestEntry] = []
        self.removed_dirs: List[str] = []
        self.planned_file_moves: List[Dict[str, str]] = []

        self.reserved_names: Dict[Path, Set[str]] = defaultdict(set)
        self.used_random_names: Set[str] = set()
        self._dup_index: Optional[DuplicateIndex] = DuplicateIndex() if detect_duplicates else None
        self._resolved_dir_cache: Dict[Path, Path] = {}
        self._ensured_dirs: Set[Path] = set()
        self._created_dirs: Set[Path] = set()
        # Skip decisions are name/pattern-based and stable within a run, but the
        # run makes 5-7 passes over the tree; memoizing avoids re-paying the
        # exclusion checks (and their resolve chains) per pass.
        self._skip_dir_cache: Dict[Path, bool] = {}
        # (dest_dir, filename) -> next collision suffix to try, so probing does
        # not restart at _1 for every file (O(n²) with n same-named files).
        self._next_collision_suffix: Dict[Tuple[Path, str], int] = {}
        self._allowed_buckets_cache: Optional[Set[str]] = None
        # "Other" dirs observed during traversal, so cleanup does not need a
        # dedicated rglob() walk over the whole tree.
        self._seen_other_dirs: Set[Path] = set()
        self._source_url_cache: Dict[Path, str] = {}

    def _visible_name(self, name: str) -> bool:
        return self.include_hidden or not name.startswith(".")

    def _is_for_deletion_name(self, name: str) -> bool:
        return name.casefold() == FOR_DELETION_DIR_NAME.casefold()

    def _is_organizer_dir(self, name: str) -> bool:
        return name.casefold() == ORGANIZER_DIR_NAME.casefold()

    def _is_duplicates_dir(self, name: str) -> bool:
        return name.casefold() == DUPLICATES_DIR_NAME.casefold()

    def _is_needs_review_dir(self, name: str) -> bool:
        return name.casefold() == NEEDS_REVIEW_DIR_NAME.casefold()

    def _resolve_dir(self, directory: Path) -> Path:
        cached = self._resolved_dir_cache.get(directory)
        if cached is None:
            try:
                cached = directory.resolve()
            except OSError:
                cached = directory
            self._resolved_dir_cache[directory] = cached
        return cached

    def _at_organize_root(self, parent_path: Path) -> bool:
        return self._resolve_dir(parent_path) == self.base

    def _should_skip_traversal_dir(self, parent_path: Path, dir_name: str) -> bool:
        key = parent_path / dir_name
        cached = self._skip_dir_cache.get(key)
        if cached is not None:
            return cached
        skip = self._compute_skip_traversal_dir(parent_path, dir_name)
        self._skip_dir_cache[key] = skip
        return skip

    def _compute_skip_traversal_dir(self, parent_path: Path, dir_name: str) -> bool:
        if dir_name.casefold() == "other":
            self._seen_other_dirs.add(parent_path / dir_name)
        if self._is_for_deletion_name(dir_name):
            return True
        if self._is_organizer_dir(dir_name):
            return True
        if self._is_needs_review_dir(dir_name):
            return True
        # Never re-organize files already staged as duplicates.
        if self._is_duplicates_dir(dir_name):
            return True
        if should_skip_traverse_dir(parent_path, dir_name, self.base, self.exclude_patterns):
            return True
        # Skip bucket directories when using in-place strategy to prevent re-organizing
        # files that were just moved into bucket folders
        if self.recursive and self.strategy == "in-place" and not self._at_organize_root(parent_path):
            if dir_name.casefold() in self._category_canonical:
                return True
        return False

    def _walk_topdown_organize(self) -> Iterator[Tuple[Path, List[str], List[str]]]:
        """Walk the tree for file moves: follow symlinks into directories, break symlink cycles via inode."""
        if not self.follow_symlinks:
            # os.walk(followlinks=False) cannot cycle; skip the per-directory
            # stat + visited-set bookkeeping entirely.
            for root, dirs, files in os.walk(self.base, topdown=True, followlinks=False):
                yield Path(root), dirs, files
            return
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

    def _probe_collision_name(self, reserved: Set[str], dest_dir: Path, original_name: str) -> str:
        """Find a free `name_N` variant, resuming N from the last probe for this
        (dir, name) so n same-named files cost O(n) probes instead of O(n²)."""
        stem, suffix = os.path.splitext(original_name)
        if suffix == ".":  # trailing-dot names: keep legacy `name_N` shape
            stem, suffix = original_name, ""
        key = (dest_dir, original_name)
        i = self._next_collision_suffix.get(key, 1)
        while True:
            candidate = f"{stem}_{i}{suffix}" if suffix else f"{original_name}_{i}"
            if candidate not in reserved:
                self._next_collision_suffix[key] = i + 1
                return candidate
            i += 1

    def _collision_safe_target(self, dest_dir: Path, original_name: str, collision_counter: str = "files") -> Path:
        self._init_reserved_dir(dest_dir)
        reserved = self.reserved_names[dest_dir]

        # If random_names is enabled, generate a random name with the original extension
        if self.random_names:
            extension = os.path.splitext(original_name)[1]
            random_name = self._generate_random_name(extension)
            reserved.add(random_name)
            return dest_dir / random_name

        if original_name not in reserved:
            reserved.add(original_name)
            return dest_dir / original_name

        self._note_collision(collision_counter)
        candidate = self._probe_collision_name(reserved, dest_dir, original_name)
        reserved.add(candidate)
        return dest_dir / candidate

    def _fast_move(self, src: Path, dst: Path) -> None:
        """Move via a single rename syscall; fall back to shutil.move for
        cross-device moves (or anything else rename cannot do)."""
        try:
            os.rename(src, dst)
        except OSError:
            shutil.move(str(src), str(dst))

    def _allowed_bucket_names(self) -> Set[str]:
        cached = self._allowed_buckets_cache
        if cached is None:
            if self.profile_buckets:
                cached = {name for name, _ in self.profile_buckets} | {"Other"}
            else:
                prof = self.profile_label if self.profile_label in ("standard", "extended") else "standard"
                cached = set(bucket_names_for_profile(prof))
            self._allowed_buckets_cache = cached
        return cached

    def _bucket_for_file(self, file_name: str, src: Optional[Path] = None) -> str:
        suffix = os.path.splitext(file_name)[1]
        ext = suffix[1:].upper() if suffix and suffix != "." else ""
        bucket = self._ext_bucket_map.get(ext, "Other") if ext else "Other"
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

    def _seed_duplicate_index(self) -> None:
        """Register files already sitting in root bucket folders as canonical copies.

        Without this, a fresh copy of an already-organized file could be encountered
        first during the walk and the previously organized file would be the one
        staged into Duplicates. Skipped for in-place mode (per-directory semantics).
        """
        if self._dup_index is None:
            return
        if self.recursive and self.strategy == "in-place":
            return
        allowed = {b.casefold() for b in self._allowed_bucket_names()}
        try:
            children = list(self.base.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.casefold() not in allowed:
                continue
            for root, _dirs, files in os.walk(child, followlinks=False):
                root_path = Path(root)
                for name in files:
                    p = root_path / name
                    try:
                        st = os.lstat(p)  # one syscall for both the symlink and size checks
                    except OSError:
                        continue
                    if not stat_module.S_ISREG(st.st_mode):
                        continue
                    if stat_is_dataless(st):
                        continue
                    self._dup_index.register(p, st.st_size)

    def _bucket_dest_dir(self, parent: Path, bucket: str, src: Path) -> Path:
        """Bucket destination for a file; adds YYYY/MM from mtime in date-buckets mode."""
        dest = parent / bucket
        if self.date_buckets:
            try:
                mt = datetime.fromtimestamp(src.stat().st_mtime)
            except (OSError, OverflowError, ValueError):
                return dest
            dest = dest / f"{mt.year:04d}" / f"{mt.month:02d}"
        return dest

    def _source_url_for(self, src: Path) -> str:
        """Return a nearby source-url sidecar value without repeatedly reading it."""
        if src.name.casefold() == "source-url.txt":
            sidecar = src
        else:
            sidecar = src.parent / "source-url.txt"
        cached = self._source_url_cache.get(sidecar)
        if cached is not None:
            return cached
        try:
            value = sidecar.read_text(encoding="utf-8", errors="replace").strip()[:4096]
        except OSError:
            value = ""
        self._source_url_cache[sidecar] = value
        return value

    def _route_file(self, src: Path, bucket_parent: Path, *, record_stats: bool = True) -> RouteDecision:
        bucket = self._bucket_for_file(src.name, src)
        if self.archive_recipe is not None:
            return self.archive_recipe.decide(source_root=self.base, source=src, bucket=bucket)
        if self.rule_set is not None:
            context = build_rule_context(
                base=self.base,
                source=src,
                bucket=bucket,
                source_url=self._source_url_for(src),
            )
            decision = self.rule_set.decide(context)
            if decision is not None:
                if decision.destination is None:
                    if record_stats:
                        self.routing_stats.left_in_place += 1
                return decision
        return RouteDecision(
            destination=self._bucket_dest_dir(bucket_parent, bucket, src),
            category=bucket,
            reason=f"File type: {bucket}",
        )

    def _record_path(self, path: Path) -> str:
        """Manifest/preview path: relative under the source root, absolute otherwise."""
        try:
            return str(path.relative_to(self.base))
        except ValueError:
            return str(path)

    def _move_one_file(
        self,
        src: Path,
        dest_dir: Path,
        bucket_parent: Optional[Path] = None,
        bucket_name: Optional[str] = None,
        *,
        reason: str = "",
        rule_id: Optional[str] = None,
        external: bool = False,
    ) -> None:
        duplicate_of: Optional[Path] = None
        dup_size: Optional[int] = None
        hardlink_dup = False
        if self._dup_index is not None:
            try:
                st = src.stat()
            except OSError:
                st = None
            # Never register cloud placeholders: hashing one would force a full
            # download of the file's content.
            if st is not None and not stat_is_dataless(st):
                dup_size = st.st_size
                duplicate_of = self._dup_index.register(src, dup_size)
            if duplicate_of is not None:
                if self.duplicates_hardlink:
                    # Keep the normal bucket destination; the move below becomes
                    # link-to-canonical + unlink, so the copy costs no space.
                    hardlink_dup = True
                else:
                    dest_dir = (bucket_parent or dest_dir.parent) / DUPLICATES_DIR_NAME

        if src.parent == dest_dir:
            return
        if self._resolve_dir(src.parent) == self._resolve_dir(dest_dir):
            return

        if duplicate_of is not None and not hardlink_dup:
            dest_dir_name = DUPLICATES_DIR_NAME
        else:
            # In date-buckets mode dest_dir ends in YYYY/MM; count by bucket.
            dest_dir_name = bucket_name or dest_dir.name
        self.ext_counts[dest_dir_name] += 1

        if not self.dry_run:
            if dest_dir not in self._ensured_dirs:
                existed_before_run = dest_dir.exists()
                dest_dir.mkdir(parents=True, exist_ok=True)
                self._ensured_dirs.add(dest_dir)
                if not existed_before_run:
                    self._created_dirs.add(dest_dir)
        else:
            self._init_reserved_dir(dest_dir)

        target = self._collision_safe_target(dest_dir, src.name)

        if len(self.planned_file_moves) < PLANNED_MOVE_SAMPLE_LIMIT:
            self.planned_file_moves.append(
                {
                    "from": self._record_path(src),
                    "to": self._record_path(target),
                    "reason": reason or f"File type: {dest_dir_name}",
                }
            )

        hardlinked = False
        if not self.dry_run:
            if hardlink_dup:
                try:
                    os.link(duplicate_of, target)
                    os.unlink(src)
                    hardlinked = True
                except OSError:
                    # Cross-volume or FS without hardlinks: keep the plain move,
                    # leaving the duplicate as a full copy in its bucket.
                    hardlinked = False
            if not hardlinked:
                self._fast_move(src, target)
            rel_src = self._record_path(src)
            rel_dst = self._record_path(target)
            self.file_moves.append(ManifestEntry(from_path=rel_src, to_path=rel_dst))
            if self._dup_index is not None and dup_size is not None and duplicate_of is None:
                # The canonical copy just moved; keep the index pointing at it.
                self._dup_index.update_location(dup_size, src, target)
        elif hardlink_dup:
            hardlinked = True  # dry run: count what a real run would hardlink

        if hardlinked:
            self.duplicate_stats.files_hardlinked += 1
        if duplicate_of is not None:
            self.duplicate_stats.files_moved += 1
            if len(self.duplicate_stats.sample_moves) < DUPLICATE_SAMPLE_LIMIT:
                self.duplicate_stats.sample_moves.append(
                    {
                        "from": self._record_path(src),
                        "to": self._record_path(target),
                        "duplicate_of": self._record_path(duplicate_of),
                    }
                )

        if rule_id:
            self.routing_stats.matched_by_rule[reason.removeprefix("Rule: ") or rule_id] += 1
        if bucket_name == NEEDS_REVIEW_DIR_NAME:
            self.routing_stats.needs_review_files += 1
        if external:
            self.routing_stats.external_moves += 1

        self.move_stats.files_moved += 1
        if self.verbose and self.move_stats.files_moved % 100 == 0:
            self._progress(f"Moved {self.move_stats.files_moved} files…\n")

    def _run_non_recursive(self) -> None:
        touched = False
        for p in list(self.base.iterdir()):
            if not p.is_file() or not self._visible_name(p.name):
                continue
            decision = self._route_file(p, self.base)
            if decision.destination is None:
                continue
            self._move_one_file(
                p, decision.destination,
                bucket_parent=self.archive_recipe.archive_root if decision.external and self.archive_recipe else self.base,
                bucket_name=decision.category,
                reason=decision.reason,
                rule_id=decision.rule_id,
                external=decision.external,
            )
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
                decision = self._route_file(src, root_path)
                if decision.destination is None:
                    continue
                self._move_one_file(
                    src, decision.destination,
                    bucket_parent=self.archive_recipe.archive_root if decision.external and self.archive_recipe else (self.base if self.rule_set else root_path),
                    bucket_name=decision.category,
                    reason=decision.reason,
                    rule_id=decision.rule_id,
                    external=decision.external,
                )
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
                decision = self._route_file(src, self.base)
                if decision.destination is None:
                    continue
                self._move_one_file(
                    src, decision.destination,
                    bucket_parent=self.archive_recipe.archive_root if decision.external and self.archive_recipe else self.base,
                    bucket_name=decision.category,
                    reason=decision.reason,
                    rule_id=decision.rule_id,
                    external=decision.external,
                )
                touched_dirs.add(root_path)

        self.move_stats.folders_touched = len(touched_dirs)

    def _maybe_normalize(self) -> None:
        if self.normalize != "standard":
            return

        # Bottom-up pass with the same directory pruning as organize (excludes, hidden, reserved dirs).
        # Snapshot each directory's child dirs BEFORE exclusion pruning: pruning
        # stops descent into bucket dirs, but they are exactly the children this
        # pass may need to case-rename. The snapshot also avoids re-listing every
        # directory (iterdir + per-entry is_dir stats) that the walk just listed.
        roots: List[Tuple[Path, List[str]]] = []
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
            child_dirs = list(dirs)
            dirs[:] = [d for d in dirs if not self._should_skip_traversal_dir(root_path, d)]
            roots.append((root_path, child_dirs))

        for parent, child_names in reversed(roots):
            if self._is_for_deletion_name(parent.name):
                continue
            if self._is_organizer_dir(parent.name):
                continue

            for child in (parent / name for name in child_names):
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
                        target_name = self._probe_collision_name(reserved, dst, target_name)

                    reserved.add(target_name)
                    self.normalize_stats.items_moved_in_merges += 1
                    if not self.dry_run:
                        dest_item = dst / target_name
                        self._fast_move(item, dest_item)
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

    def _simulate_empty_dir_collection(self) -> Optional[EmptyDirStats]:
        """Predict For Deletion staging for dry runs without touching the disk.

        Builds an in-memory snapshot of the tree in one scandir walk, replays the
        organize file moves on it, then runs the same collection logic on the model.
        (Normalization-only folder merges are not replayed; the extremely rare case
        of two empty case-variant bucket dirs can preview one extra folder.)
        """
        try:
            root = self._build_sim_tree()
            self._sim_organize(root)
            return self._sim_collect_empty_dirs(root)
        except Exception:
            return None

    def _build_sim_tree(self) -> "_SimDir":
        visited: Set[Tuple[int, int]] = set()
        root = _SimDir(self.base, None, False)

        def build(node: _SimDir) -> None:
            try:
                st = os.stat(node.path, follow_symlinks=False)
                key = (st.st_dev, st.st_ino)
                if key in visited:
                    return
                visited.add(key)
            except OSError:
                return
            try:
                entries = list(os.scandir(node.path))
            except OSError:
                return
            for entry in entries:
                try:
                    is_link = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                if is_dir:
                    child = _SimDir(node.path / entry.name, node, is_link)
                    node.subdirs[entry.name] = child
                    if self.follow_symlinks or not is_link:
                        build(child)
                else:
                    node.files[entry.name] = is_link

        build(root)
        return root

    def _sim_get_or_create_subdir(self, node: "_SimDir", name: str) -> "_SimDir":
        for existing_name, child in node.subdirs.items():
            if existing_name.casefold() == name.casefold():
                return child
        child = _SimDir(node.path / name, node, False)
        node.subdirs[name] = child
        return child

    def _sim_get_or_create_path(self, root: "_SimDir", destination: Path) -> Optional["_SimDir"]:
        try:
            relative = destination.relative_to(self.base)
        except ValueError:
            return None
        node = root
        for part in relative.parts:
            node = self._sim_get_or_create_subdir(node, part)
        return node

    def _sim_walk(self, root: "_SimDir") -> Iterator[Tuple["_SimDir", List[str]]]:
        """Topdown model walk with the same pruning as the organize walk."""
        stack = [root]
        while stack:
            node = stack.pop()
            dir_names = list(node.subdirs)
            file_names = list(node.files)
            if not self.include_hidden:
                dir_names = [d for d in dir_names if self._visible_name(d)]
                file_names = [f for f in file_names if self._visible_name(f)]
            dir_names = [d for d in dir_names if not self._should_skip_traversal_dir(node.path, d)]
            yield node, file_names
            for d in dir_names:
                stack.append(node.subdirs[d])

    def _sim_organize(self, root: "_SimDir") -> None:
        """Replay the organize move phase on the model (removal/placement only)."""
        if not self.recursive:
            for fn in list(root.files):
                if not self._visible_name(fn):
                    continue
                decision = self._route_file(root.path / fn, self.base, record_stats=False)
                if decision.destination is None:
                    continue
                dest = None if decision.external else self._sim_get_or_create_path(root, decision.destination)
                del root.files[fn]
                if dest is not None:
                    dest.files[fn] = False
            return

        for node, file_names in self._sim_walk(root):
            for fn in file_names:
                parent = node.path if self.strategy == "in-place" else self.base
                decision = self._route_file(node.path / fn, parent, record_stats=False)
                if decision.destination is None:
                    continue
                if decision.external:
                    del node.files[fn]
                    continue
                dest = self._sim_get_or_create_path(root, decision.destination)
                if dest is None:
                    continue
                if dest is node or self._resolve_dir(node.path) == self._resolve_dir(dest.path):
                    continue
                del node.files[fn]
                if dest is not None:
                    dest.files[fn] = False

    def _sim_inspect_empty_dir_tree(self, node: "_SimDir") -> Tuple[bool, List["_SimDir"]]:
        if path_excluded(node.path, self.base, self.exclude_patterns):
            return False, []
        if node.is_symlink:
            return False, []
        if not node.subdirs and not node.files:
            return True, []

        collectable = True
        topmost: List[_SimDir] = []

        for name, child in node.subdirs.items():
            if self._is_for_deletion_name(name) or self._is_organizer_dir(name):
                collectable = False
                continue
            if self._should_skip_traversal_dir(node.path, name):
                collectable = False
                continue
            if not self.include_hidden and not self._visible_name(name):
                collectable = False
                continue
            if child.is_symlink:
                collectable = False
                continue
            child_collectable, child_topmost = self._sim_inspect_empty_dir_tree(child)
            if child_collectable:
                topmost.append(child)
            else:
                collectable = False
                topmost.extend(child_topmost)

        if node.files:
            collectable = False

        if collectable:
            return True, []
        return False, topmost

    def _sim_find_empty_dir_candidates(self, root: "_SimDir") -> List["_SimDir"]:
        candidates: List[_SimDir] = []
        seen: Set[int] = set()
        for name in sorted(root.subdirs, key=str.lower):
            child = root.subdirs[name]
            if child.is_symlink:
                continue
            if not self._visible_name(name):
                continue
            if self._is_for_deletion_name(name) or self._is_organizer_dir(name):
                continue
            if self._should_skip_traversal_dir(self.base, name):
                continue
            child_collectable, child_topmost = self._sim_inspect_empty_dir_tree(child)
            if child_collectable:
                paths_to_add = [child]
            elif self.recursive:
                paths_to_add = child_topmost
            else:
                paths_to_add = []
            for cand in paths_to_add:
                if id(cand) in seen:
                    continue
                seen.add(id(cand))
                candidates.append(cand)
        return candidates

    def _sim_collect_empty_dirs(self, root: "_SimDir") -> EmptyDirStats:
        stats = EmptyDirStats()
        fd_node: Optional[_SimDir] = None
        for name, child in root.subdirs.items():
            if self._is_for_deletion_name(name):
                fd_node = child
                break

        reserved: Optional[Set[str]] = None
        for _ in range(500):
            candidates = self._sim_find_empty_dir_candidates(root)
            if not candidates:
                break
            if fd_node is None:
                fd_node = _SimDir(self.base / FOR_DELETION_DIR_NAME, root, False)
                root.subdirs[FOR_DELETION_DIR_NAME] = fd_node
            if reserved is None:
                reserved = set(fd_node.subdirs) | set(fd_node.files)
            for cand in candidates:
                name = cand.path.name
                final_name = name
                if final_name in reserved:
                    stats.name_collisions_resolved += 1
                    final_name = self._probe_collision_name(reserved, fd_node.path, name)
                reserved.add(final_name)
                if len(stats.sample_moves) < EMPTY_DIR_SAMPLE_LIMIT:
                    stats.sample_moves.append(
                        {
                            "from": str(cand.path.relative_to(self.base)),
                            "to": str((fd_node.path / final_name).relative_to(self.base)),
                        }
                    )
                parent = cand.parent
                if parent is not None:
                    for key, val in list(parent.subdirs.items()):
                        if val is cand:
                            del parent.subdirs[key]
                            break
                cand.parent = fd_node
                fd_node.subdirs[final_name] = cand
                stats.folders_moved += 1
        return stats

    def _inspect_empty_dir_tree(self, directory: Path) -> tuple[bool, List[Path]]:
        if path_excluded(directory, self.base, self.exclude_patterns):
            return False, []
        if os.path.islink(directory):
            return False, []

        # scandir DirEntry answers is_dir/is_symlink from d_type without extra
        # stat syscalls; iterdir + per-entry Path checks paid 2-3 per entry.
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
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
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                collectable = False
                continue
            if is_dir and self._should_skip_traversal_dir(directory, entry.name):
                collectable = False
                continue

            if not self.include_hidden and not self._visible_name(entry.name):
                collectable = False
                continue

            if is_symlink:
                collectable = False
                continue

            if is_dir:
                child_path = Path(entry.path)
                child_collectable, child_topmost = self._inspect_empty_dir_tree(child_path)
                if child_collectable:
                    topmost_children.append(child_path)
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

        try:
            with os.scandir(self.base) as it:
                children = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            return []

        for entry in children:
            try:
                if not entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                    continue
            except OSError:
                continue
            if not self._visible_name(entry.name):
                continue
            if self._is_for_deletion_name(entry.name):
                continue
            if self._is_organizer_dir(entry.name):
                continue
            if self._should_skip_traversal_dir(self.base, entry.name):
                continue

            child = Path(entry.path)
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
                self._fast_move(src_dir, target)
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

            # A directory containing hidden files is not empty. Earlier behavior
            # purged those files when include_hidden=False, which contradicted the
            # option's promise to skip them and could destroy user data.
            if entries:
                continue

            if not self.dry_run:
                try:
                    if any(root_path.iterdir()):
                        continue
                    self.removed_dirs.append(str(root_path.relative_to(self.base)))
                    root_path.rmdir()
                except OSError:
                    continue
            self.empty_dirs_removed += 1

    def _cleanup_dsstore_files(self) -> None:
        """Remove .DS_Store files (both named and randomly renamed)."""
        dsstore_files_removed = 0

        for root, _dirs, files in os.walk(self.base, followlinks=False):
            for name in files:
                is_dsstore = False
                if name == ".DS_Store" or name.endswith(".DS_Store"):
                    is_dsstore = True
                elif "." not in name and RANDOM_NAME_CANDIDATE_PATTERN.match(name):
                    # Only content-sniff extensionless files that look like the
                    # random-rename output; opening every extensionless file made
                    # this pass dominate large runs.
                    try:
                        with open(os.path.join(root, name), "rb") as f:
                            header = f.read(8)
                        if header == b"\x00\x00\x00\x01Bud1":
                            is_dsstore = True
                    except OSError:
                        pass

                if is_dsstore:
                    if not self.dry_run:
                        try:
                            os.unlink(os.path.join(root, name))
                            dsstore_files_removed += 1
                        except OSError:
                            pass
                    else:
                        dsstore_files_removed += 1

        if dsstore_files_removed > 0:
            action = "Would remove" if self.dry_run else "Removed"
            print(f"{action} {dsstore_files_removed} .DS_Store file(s)")

    def _cleanup_empty_other_bucket(self) -> None:
        """Remove only genuinely empty Other directories.

        Never infer emptiness from this run's move counters: an existing bucket can
        contain files even when the current run moved nothing into it.
        """
        # Only remove an empty bucket created by this run. A pre-existing empty
        # directory named Other may be meaningful to the user and is not ours to
        # clean up.
        candidates = (d for d in self._created_dirs if d.name.casefold() == "other")
        for other_dir in sorted(candidates):
            if other_dir.is_dir():
                try:
                    if not any(other_dir.iterdir()) and not self.dry_run:
                        other_dir.rmdir()
                except OSError:
                    pass

    def _verify(self) -> Dict[str, object]:
        root_visible = 0
        root_all = 0
        for p in self.base.iterdir():
            if p.is_file():
                root_all += 1
                if self._visible_name(p.name):
                    root_visible += 1

        # One walk computes both metrics. The noncanonical-dir check needs to
        # descend into bucket dirs while the in-place file count must not; the
        # `counted` set tracks which visited dirs count toward the file metric
        # instead of pruning them from the walk.
        noncanonical_dirs = []
        remaining_unorganized_visible_files = None
        checked_non_bucket_directories = None
        in_place = self.recursive and self.strategy == "in-place"
        if in_place:
            remaining_unorganized_visible_files = 0
            checked_non_bucket_directories = 0

        counted: Set[Path] = {self.base}
        for root_path, dirs, files in self._walk_topdown_organize():
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
            dirs[:] = [d for d in dirs if not self._is_for_deletion_name(d)]
            dirs[:] = [d for d in dirs if not self._is_organizer_dir(d)]
            for d in dirs:
                c = self._canonical_folder_name(d)
                if c is not None and c != d:
                    noncanonical_dirs.append(str(root_path / d))

            if in_place and root_path in counted:
                checked_non_bucket_directories += 1
                if self.include_hidden:
                    remaining_unorganized_visible_files += len(files)
                else:
                    remaining_unorganized_visible_files += sum(
                        1 for f in files if self._visible_name(f)
                    )
                for d in dirs:
                    if not self._should_skip_traversal_dir(root_path, d):
                        counted.add(root_path / d)

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
            external_destinations=self.routing_stats.external_moves > 0,
        )
        helper = Path(__file__).resolve().parent / "organize_by_filetype.py"
        info = write_manifest_files(
            self.base,
            manifest,
            helper_script=helper,
            dry_run=self.dry_run,
            create_backup=self.create_backup,
        )
        if (
            info
            and self.archive_recipe is not None
            and self.routing_stats.external_moves > 0
            and not self.dry_run
        ):
            archive_info = write_manifest_files(
                self.archive_recipe.archive_root,
                manifest,
                helper_script=helper,
                dry_run=False,
                create_backup=self.create_backup,
            )
            if archive_info:
                info["archive_manifest"] = archive_info["manifest"]
        return info


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
            
            # Skip organizer, deletion, and duplicate-staging directories
            if (
                self._is_organizer_dir(root_path.name)
                or self._is_for_deletion_name(root_path.name)
                or self._is_duplicates_dir(root_path.name)
                or self._is_needs_review_dir(root_path.name)
            ):
                dirs[:] = []
                continue
                
            if not self.include_hidden:
                dirs[:] = [d for d in dirs if self._visible_name(d)]
                files = [f for f in files if self._visible_name(f)]
                
            for filename in files:
                src = root_path / filename
                try:
                    st = os.lstat(src)  # one syscall replaces is_file()+is_symlink()
                except OSError:
                    continue
                if not stat_module.S_ISREG(st.st_mode):
                    continue
                # Names from earlier runs already look random; reserving them means
                # _generate_random_name can never emit a name that exists on disk,
                # so no per-file dest.exists() stat is needed below.
                if RANDOM_NAME_PATTERN.match(filename) or RANDOM_NAME_CANDIDATE_PATTERN.match(filename):
                    self.used_random_names.add(filename)
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

        self._seed_duplicate_index()

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
        # .DS_Store cleanup must precede the empty-dir passes: a folder holding
        # only a .DS_Store is empty once the file is gone, and with
        # include_hidden=True nothing else would ever purge it.
        self._cleanup_dsstore_files()
        self._maybe_collect_empty_dirs()
        self._remove_empty_subdirs()
        self._cleanup_empty_other_bucket()

        # Rename all files after organization if requested
        rename_stats = {}
        if self.random_names_after_organize:
            rename_stats = self._rename_all_files_after_organize()

        manifest_info = self.save_manifest()
        if manifest_info and not self.dry_run:
            cleanup_old_manifests(self.base, days_to_keep=7)
            if self.archive_recipe is not None:
                cleanup_old_manifests(self.archive_recipe.archive_root, days_to_keep=7)
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
            "duplicates": {
                "enabled": self.detect_duplicates,
                "destination_dir_name": DUPLICATES_DIR_NAME if self.detect_duplicates else None,
                "hardlink": self.duplicates_hardlink,
                "files_moved": self.duplicate_stats.files_moved,
                "files_hardlinked": self.duplicate_stats.files_hardlinked,
                "sample_moves": self.duplicate_stats.sample_moves,
            },
            "date_buckets": self.date_buckets,
            "routing": {
                "rules_file": str(self.rule_set.source_path) if self.rule_set and self.rule_set.source_path else None,
                "unmatched": self.rule_set.unmatched if self.rule_set else None,
                "matched_by_rule": dict(sorted(self.routing_stats.matched_by_rule.items())),
                "needs_review_files": self.routing_stats.needs_review_files,
                "left_in_place": self.routing_stats.left_in_place,
                "archive_root": str(self.archive_recipe.archive_root) if self.archive_recipe else None,
                "external_moves": self.routing_stats.external_moves,
            },
            "planned_moves": self.planned_file_moves,
            "planned_moves_truncated": self.move_stats.files_moved > len(self.planned_file_moves),
            "rename_after_organize": {
                "enabled": self.random_names_after_organize,
                "files_renamed": rename_stats.get("files_renamed", 0),
                "files_skipped": rename_stats.get("files_skipped", 0),
                "errors": rename_stats.get("errors", []),
            },
            "verification": self._verify(),
            "backup_manifest": manifest_info.get("manifest") if manifest_info else None,
            "archive_backup_manifest": manifest_info.get("archive_manifest") if manifest_info else None,
            "restore_script": manifest_info.get("restore_script") if manifest_info else None,
            "restore_sh": manifest_info.get("restore_sh") if manifest_info else None,
            "restore_cli": manifest_info.get("restore_cli") if manifest_info else None,
            "ocr_index": ocr_path,
        }
        return summary
