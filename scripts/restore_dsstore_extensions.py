#!/usr/bin/env python3
"""Restore .DS_Store extensions for randomly renamed .DS_Store files."""

import shutil
from pathlib import Path


def restore_dsstore_extensions(root: Path, dry_run: bool = True) -> dict:
    """Find .DS_Store files with random names and restore their extensions."""
    stats = {
        "files_checked": 0,
        "files_renamed": 0,
        "files_skipped": 0,
        "errors": [],
    }

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        
        # Skip files that already have extensions or are already .DS_Store files
        if file_path.suffix or file_path.name == '.DS_Store':
            stats["files_skipped"] += 1
            continue
            
        stats["files_checked"] += 1
        
        try:
            # Check if it's a .DS_Store file by reading the header
            with open(file_path, 'rb') as f:
                header = f.read(8)
                
            # .DS_Store files start with "Bud1" (bytes: 00 00 00 01 42 75 64 31)
            if len(header) >= 8 and header == b'\x00\x00\x00\x01Bud1':
                new_path = file_path.with_suffix('.DS_Store')
                
                print(f"{'Would rename' if dry_run else 'Renaming'}: {file_path.name} -> {new_path.name}")
                stats["files_renamed"] += 1
                
                if not dry_run:
                    shutil.move(str(file_path), str(new_path))
            else:
                stats["files_skipped"] += 1
                
        except Exception as e:
            error = f"Error checking {file_path}: {e}"
            stats["errors"].append(error)
            print(f"ERROR: {error}")

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Restore .DS_Store extensions")
    parser.add_argument("path", help="Root path to search for .DS_Store files")
    parser.add_argument("--execute", action="store_true", help="Actually rename files (default: dry-run)")
    args = parser.parse_args()

    root = Path(args.path).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory")
        exit(1)

    print(f"Scanning {root} for .DS_Store files with random names...")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print()

    stats = restore_dsstore_extensions(root, dry_run=not args.execute)

    print()
    print("Summary:")
    print(f"  Files checked: {stats['files_checked']}")
    print(f"  Files renamed: {stats['files_renamed']}")
    print(f"  Files skipped: {stats['files_skipped']}")
    print(f"  Errors: {len(stats['errors'])}")

    if stats['errors']:
        print("\nErrors:")
        for error in stats['errors']:
            print(f"  - {error}")
