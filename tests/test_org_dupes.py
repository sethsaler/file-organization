#!/usr/bin/env python3
"""Tests for org_dupes (size-first grouping with lazy content hashing)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import org_dupes
from org_dupes import DuplicateIndex, hash_file, stat_is_dataless


def _blake2b(content: bytes) -> str:
    return hashlib.blake2b(content, digest_size=32).hexdigest()


def test_hash_file_matches_blake2b(tmp_path: Path):
    p = tmp_path / "a.bin"
    content = b"hello world" * 100
    p.write_bytes(content)
    assert hash_file(p) == _blake2b(content)


def test_hash_file_missing_returns_none(tmp_path: Path):
    assert hash_file(tmp_path / "missing.bin") is None


def test_stat_is_dataless_flags():
    dataless_bit = org_dupes._SF_DATALESS
    assert stat_is_dataless(SimpleNamespace(st_flags=dataless_bit, st_blocks=8, st_size=10))
    assert not stat_is_dataless(SimpleNamespace(st_flags=0, st_blocks=8, st_size=10))
    # No st_flags attribute at all (e.g. non-macOS stat): falls through to the heuristic.
    assert not stat_is_dataless(SimpleNamespace(st_blocks=8, st_size=10))


def test_stat_is_dataless_zero_block_heuristic():
    # Large file with zero allocated blocks looks like a cloud placeholder.
    assert stat_is_dataless(SimpleNamespace(st_flags=0, st_blocks=0, st_size=100_000))
    # Tiny files can legitimately inline to zero blocks.
    assert not stat_is_dataless(SimpleNamespace(st_flags=0, st_blocks=0, st_size=100))


def test_register_unique_sizes_never_hashes(tmp_path: Path, monkeypatch):
    calls: list = []
    real_hash = org_dupes.hash_file

    def spy(path):
        calls.append(path)
        return real_hash(path)

    monkeypatch.setattr(org_dupes, "hash_file", spy)

    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"12345")
    b.write_bytes(b"1234567")
    assert idx.register(a, a.stat().st_size) is None
    assert idx.register(b, b.stat().st_size) is None
    # No size collision means no file was ever opened.
    assert calls == []


def test_zero_byte_files_are_never_duplicates(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "empty1"
    b = tmp_path / "empty2"
    a.write_bytes(b"")
    b.write_bytes(b"")
    assert idx.register(a, 0) is None
    assert idx.register(b, 0) is None


def test_identical_content_returns_canonical(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    for p in (a, b, c):
        p.write_bytes(b"same content")

    assert idx.register(a, a.stat().st_size) is None  # first: canonical
    assert idx.register(b, b.stat().st_size) == a
    assert idx.register(c, c.stat().st_size) == a


def test_same_size_different_content_not_duplicates(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    a.write_bytes(b"AAAAAAAA")
    b.write_bytes(b"BBBBBBBB")
    c.write_bytes(b"BBBBBBBB")

    assert idx.register(a, a.stat().st_size) is None
    assert idx.register(b, b.stat().st_size) is None  # same size, different hash
    assert idx.register(c, c.stat().st_size) == b  # duplicate of b, not a


def test_registering_same_path_twice_is_not_a_duplicate(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"dup content")
    b.write_bytes(b"dup content")

    assert idx.register(a, a.stat().st_size) is None
    # Before hashing starts (single-path slot).
    assert idx.register(a, a.stat().st_size) is None
    assert idx.register(b, b.stat().st_size) == a
    # After hashing (dict slot): canonical re-registered is still not a dup.
    assert idx.register(a, a.stat().st_size) is None


def test_unreadable_file_is_not_registered_as_duplicate(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    a.write_bytes(b"content!")
    missing = tmp_path / "missing.bin"

    assert idx.register(a, a.stat().st_size) is None
    # Same size as a but unreadable: hash fails, treated as non-duplicate.
    assert idx.register(missing, a.stat().st_size) is None


def test_update_location_single_slot(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    a2 = tmp_path / "a-moved.bin"
    b = tmp_path / "b.bin"
    content = b"unique-size-content-here"
    a.write_bytes(content)
    a.rename(a2)
    b.write_bytes(content)

    size = len(content)
    assert idx.register(a, size) is None
    idx.update_location(size, a, a2)
    # Later identical file must point at the moved canonical.
    assert idx.register(b, size) == a2


def test_update_location_dict_slot(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    a2 = tmp_path / "a-moved.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    content = b"shared content"
    for p in (a, b, c):
        p.write_bytes(content)

    size = len(content)
    assert idx.register(a, size) is None
    assert idx.register(b, size) == a  # forces hashing / dict slot

    # Canonical copy moves on disk; index must follow so later lookups hash a2.
    a.rename(a2)
    idx.update_location(size, a, a2)
    assert idx.register(c, size) == a2


def test_update_location_unknown_size_is_noop(tmp_path: Path):
    idx = DuplicateIndex()
    a = tmp_path / "a.bin"
    a.write_bytes(b"x")
    # Must not raise for sizes/paths never registered.
    idx.update_location(12345, a, tmp_path / "elsewhere")
    assert idx.register(a, 1) is None
