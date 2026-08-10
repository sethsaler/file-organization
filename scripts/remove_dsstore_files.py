#!/usr/bin/env python3
"""Remove all .DS_Store files (both randomly named and with extensions)."""

from pathlib import Path


def remove_dsstore_files(root: Path, dry_run: bool = True) -> dict:
    """Find and remove all .DS_Store files."""
    stats = {
        "files_checked": 0,
        "files_removed": 0,
        "files_skipped": 0,
        "errors": [],
    }

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
            
        stats["files_checked"] += 1
        
        # Check if it's a .DS_Store file (either by name or by content)
        is_dsstore = False
        
        # Check by name
        if file_path.name == '.DS_Store' or file_path.suffix == '.DS_Store':
            is_dsstore = True
        # Check by content for randomly named .DS_Store files
        elif not file_path.suffix:
            try:
                with open(file_path, 'rb') as f:
                    header = f.read(8)
                if len(header) >= 8 and header == b'\x00\x00\x00\x01Bud1':
                    is_dsstore = True
            except Exception:
                pass
        
        if is_dsstore:
            print(f"{'Would remove' if dry_run else 'Removing'}: {file_path}")
            stats["files_removed"] += 1
            
            if not dry_run:
                try:
                    file_path.unlink()
                except Exception as e:
                    error = f"Error removing {file_path}: {e}"
                    stats["errors"].append(error)
                    print(f"ERROR: {error}")
        else:
            stats["files_skipped"] += 1

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Remove .DS_Store files")
    parser.add_argument("path", help="Root path to search for .DS_Store files")
    parser.add_argument("--execute", action="store_true", help="Actually remove files (default: dry-run)")
    args = parser.parse_args()

    root = Path(args.path).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory")
        exit(1)

    print(f"Scanning {root} for .DS_Store files...")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print()

    stats = remove_dsstore_files(root, dry_run=not args.execute)

    print()
    print("Summary:")
    print(f"  Files checked: {stats['files_checked']}")
    print(f"  Files removed: {stats['files_removed']}")
    print(f"  Files skipped: {stats['files_skipped']}")
    print(f"  Errors: {len(stats['errors'])}")

    if stats['errors']:
        print("\nErrors:")
        for error in stats['errors']:
            print(f"  - {error}")
