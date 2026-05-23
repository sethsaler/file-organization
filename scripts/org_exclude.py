#!/usr/bin/env python3
"""Path exclusion patterns for organizer traversal."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List, Optional, Sequence, Set

DEFAULT_DIR_EXCLUDES: Set[str] = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    ".svn", ".hg", "dist", "build", ".next", ".turbo",
}


def merge_exclude_patterns(user_patterns: Sequence[str], *, use_defaults: bool) -> List[str]:
    patterns: List[str] = []
    if use_defaults:
        patterns.extend(sorted(DEFAULT_DIR_EXCLUDES))
    patterns.extend(p.strip() for p in user_patterns if p and p.strip())
    return patterns


def dir_name_excluded(dir_name: str, patterns: Sequence[str]) -> bool:
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(dir_name, pat) or fnmatch.fnmatchcase(dir_name, pat):
            return True
        if dir_name == pat or dir_name.casefold() == pat.casefold():
            return True
    return False


def path_excluded(path: Path, base: Path, patterns: Sequence[str]) -> bool:
    try:
        rel = path.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return False
    parts = list(rel.parts)
    rel_s = "/".join(parts)
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(rel_s, pat) or fnmatch.fnmatchcase(rel_s, pat):
            return True
        for part in parts:
            if dir_name_excluded(part, (pat,)):
                return True
    return False


def should_skip_traverse_dir(parent_path: Path, dir_name: str, base: Path, patterns: Sequence[str]) -> bool:
    if dir_name_excluded(dir_name, patterns):
        return True
    return path_excluded(parent_path / dir_name, base, patterns)
