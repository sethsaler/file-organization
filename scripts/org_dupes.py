#!/usr/bin/env python3
"""Incremental duplicate detection: size-first grouping with lazy content hashing.

Files are only read (hashed) when at least two files share the same byte size,
so trees with mostly unique sizes pay a single os.stat per file and nothing more.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
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


class HashCache:
    """Disposable content hashes, validated against identity and nanosecond times.

    Stored outside organized trees. Corrupt/unwritable caches are harmless misses.
    A before/after stat prevents caching files changed while they were read.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = {}
        self.dirty = False
        self.hits = 0
        self.misses = 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == 1 and isinstance(data.get("entries"), dict):
                self.entries = {
                    key: entry for key, entry in data["entries"].items()
                    if isinstance(entry, dict) and isinstance(entry.get("stat"), list)
                    and len(entry["stat"]) == 5 and all(isinstance(n, int) for n in entry["stat"])
                    and isinstance(entry.get("digest"), str) and len(entry["digest"]) == 64
                    and all(c in "0123456789abcdef" for c in entry["digest"])
                }
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def fingerprint(path: Path):
        try:
            st = path.lstat()
            if not _stat.S_ISREG(st.st_mode) or stat_is_dataless(st):
                return None
            return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
        except OSError:
            return None

    def get(self, path: Path) -> Optional[str]:
        before = self.fingerprint(path)
        if before is None:
            return None
        key = str(path)
        entry = self.entries.get(key)
        if isinstance(entry, dict) and entry.get("stat") == before:
            digest = entry.get("digest")
            if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                self.hits += 1
                return digest
        self.misses += 1
        digest = hash_file(path)
        if digest is None or self.fingerprint(path) != before:
            return None
        self.entries[key] = {"stat": before, "digest": digest}
        self.dirty = True
        return digest

    def moved(self, old: Path, new: Path) -> None:
        entry = self.entries.pop(str(old), None)
        if entry is None:
            return
        self.dirty = True
        current = self.fingerprint(new)
        if (current is not None and isinstance(entry, dict)
                and isinstance(entry.get("stat"), list) and entry["stat"][:4] == current[:4]):
            self.entries[str(new)] = {"stat": current, "digest": entry["digest"]}

    def save(self) -> None:
        if not self.dirty:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Prune disappeared or changed files; never read their contents here.
            self.entries = {key: entry for key, entry in self.entries.items()
                            if isinstance(entry, dict) and self.fingerprint(Path(key)) == entry.get("stat")}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".hash-cache-", delete=False) as stream:
                temporary = stream.name
                json.dump({"version": 1, "entries": self.entries}, stream)
            os.replace(temporary, self.path)
            self.dirty = False
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


class DuplicateIndex:
    """Registers files one at a time and reports which are duplicates of earlier ones.

    The first file registered with a given content is the canonical copy; later
    identical files are reported as duplicates of it. Zero-byte files are never
    treated as duplicates of each other (they are trivially identical but usually
    intentional placeholders).
    """

    def __init__(self, cache: Optional[HashCache] = None) -> None:
        self.cache = cache
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
            first_hash = self.cache.get(slot) if self.cache else hash_file(slot)
            hashes: Dict[str, Path] = {}
            if first_hash is not None:
                hashes[first_hash] = slot
                self._digest_by_path[slot] = first_hash
            self._by_size[size] = hashes
            slot = hashes

        if not isinstance(slot, dict):  # pragma: no cover - defensive
            return None

        digest = self.cache.get(path) if self.cache else hash_file(path)
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
        if self.cache is not None:
            self.cache.moved(old, new)
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
