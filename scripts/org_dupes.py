#!/usr/bin/env python3
"""Incremental duplicate detection: size-first grouping with lazy content hashing.

Files are only read (hashed) when at least two files share the same byte size,
so trees with mostly unique sizes pay a single os.stat per file and nothing more.
"""

from __future__ import annotations

import hashlib
import stat as _stat
from pathlib import Path
from typing import Dict, Optional, Union

_CHUNK_SIZE = 1 << 20  # 1 MiB

# macOS marks placeholder files whose content lives only in the cloud
# (undownloaded iCloud Drive items) with SF_DATALESS in st_flags.
_SF_DATALESS = getattr(_stat, "SF_DATALESS", 0x40000000)


def stat_is_dataless(st) -> bool:
    """True for cloud-placeholder files with no local content (e.g. undownloaded
    iCloud files) — reading one forces a full download, so duplicate hashing
    must never touch them."""
    if getattr(st, "st_flags", 0) & _SF_DATALESS:
        return True
    # Fallback heuristic: a sizable file with zero allocated blocks. Tiny files
    # can legitimately inline to zero blocks, so require a real size.
    return getattr(st, "st_blocks", None) == 0 and st.st_size > 4096


def hash_file(path: Path) -> Optional[str]:
    """Content hash of a file, or None when unreadable."""
    h = hashlib.blake2b(digest_size=32)
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


class DuplicateIndex:
    """Registers files one at a time and reports which are duplicates of earlier ones.

    The first file registered with a given content is the canonical copy; later
    identical files are reported as duplicates of it. Zero-byte files are never
    treated as duplicates of each other (they are trivially identical but usually
    intentional placeholders).
    """

    def __init__(self) -> None:
        # size -> Path (only one file of this size seen, not hashed yet)
        #      -> dict hash -> canonical Path (two or more files of this size)
        self._by_size: Dict[int, Union[Path, Dict[str, Path]]] = {}
        # canonical Path -> digest, so update_location is O(1) instead of a
        # linear scan over every same-size hash entry.
        self._digest_by_path: Dict[Path, str] = {}

    def register(self, path: Path, size: int) -> Optional[Path]:
        """Add a file; return the canonical original if this file duplicates one."""
        if size == 0:
            return None

        slot = self._by_size.get(size)
        if slot is None:
            self._by_size[size] = path
            return None

        if isinstance(slot, Path):
            if slot == path:
                return None
            first_hash = hash_file(slot)
            hashes: Dict[str, Path] = {}
            if first_hash is not None:
                hashes[first_hash] = slot
                self._digest_by_path[slot] = first_hash
            self._by_size[size] = hashes
            slot = hashes

        if not isinstance(slot, dict):  # pragma: no cover - defensive
            return None

        digest = hash_file(path)
        if digest is None:
            return None
        original = slot.get(digest)
        if original is None:
            slot[digest] = path
            self._digest_by_path[path] = digest
            return None
        if original == path:
            return None
        return original

    def update_location(self, size: int, old: Path, new: Path) -> None:
        """Record that a registered file moved, so later lookups hash the right path."""
        slot = self._by_size.get(size)
        if isinstance(slot, Path):
            if slot == old:
                self._by_size[size] = new
            return
        if isinstance(slot, dict):
            digest = self._digest_by_path.pop(old, None)
            if digest is not None and slot.get(digest) == old:
                slot[digest] = new
                self._digest_by_path[new] = digest
                return
            for digest, stored in slot.items():  # fallback: unhashed/legacy entries
                if stored == old:
                    slot[digest] = new
                    self._digest_by_path[new] = digest
                    return
