#!/usr/bin/env python3
"""Compare full and incremental runs on isolated, generated file trees.

No user folders are organized. Times are synthetic local filesystem measurements.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
from pathlib import Path

from org_dupes import HashCache
from org_organizer import Organizer


def run(root, changed_paths=None):
    return Organizer(base=root, recursive=True, strategy="flatten-root", include_hidden=False,
                     normalize="standard", collect_empty_dirs=True, dry_run=False,
                     create_backup=False, changed_paths=changed_paths).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=3000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.files < 1 or args.repeats < 1:
        parser.error("--files and --repeats must be positive")
    full, incremental, cold, warm = [], [], [], []
    with tempfile.TemporaryDirectory(prefix="organizer-benchmark-") as temporary:
        base = Path(temporary)
        root = base / "library"
        for i in range(args.files):
            directory = root / "Images" / f"album-{i % 100:03d}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"photo-{i}.jpg").write_bytes(b"test image")
        # Full and incremental see equivalent trees and the same one-file arrival.
        # Existing nested images are flattened once before measuring steady state.
        run(root)
        for i in range(args.repeats):
            for measurements, use_incremental in ((full, False), (incremental, True)):
                source = root / f"arrival-{i}-{use_incremental}.jpg"
                source.write_bytes(b"new photo")
                result = run(root, [source] if use_incremental else None)
                assert result["files_moved"] == 1
                measurements.append(result["phase_seconds"]["total"])
        content = base / "hash-input.bin"
        content.write_bytes(b"benchmark payload" * (1024 * 1024))
        import time
        for i in range(args.repeats):
            cache_path = base / f"cache-{i}.json"
            cache = HashCache(cache_path)
            start = time.perf_counter()
            digest = cache.get(content)
            cold.append(time.perf_counter() - start)
            cache.save()
            cache = HashCache(cache_path)
            start = time.perf_counter()
            assert cache.get(content) == digest
            warm.append(time.perf_counter() - start)
    print(json.dumps({
        "synthetic_files": args.files, "repeats": args.repeats,
        "full_run_median_seconds": statistics.median(full),
        "incremental_run_median_seconds": statistics.median(incremental),
        "incremental_speedup": statistics.median(full) / statistics.median(incremental),
        "cold_hash_median_seconds": statistics.median(cold),
        "cached_hash_median_seconds": statistics.median(warm),
    }, indent=2))


if __name__ == "__main__":
    main()
