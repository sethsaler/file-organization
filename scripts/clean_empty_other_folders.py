#!/usr/bin/env python3
"""Clean up empty 'Other' folders that contain no visible files."""

import shutil
from pathlib import Path


def clean_empty_other_folders(root: Path, dry_run: bool = True) -> dict:
    """Remove 'Other' folders that contain no visible files."""
    stats = {
        "folders_checked": 0,
        "folders_removed": 0,
        "folders_with_files": 0,
        "errors": [],
    }

    for other_dir in root.rglob("Other"):
        if not other_dir.is_dir():
            continue

        stats["folders_checked"] += 1
        
        try:
            # Check for visible files only
            visible_files = [f for f in other_dir.iterdir() if not f.name.startswith(".")]
            
            if not visible_files:
                print(f"{'Would remove' if dry_run else 'Removing'}: {other_dir}")
                stats["folders_removed"] += 1
                
                if not dry_run:
                    shutil.rmtree(other_dir)
            else:
                stats["folders_with_files"] += 1
                print(f"Keeping (has {len(visible_files)} visible files): {other_dir}")
                
        except Exception as e:
            error = f"Error checking {other_dir}: {e}"
            stats["errors"].append(error)
            print(f"ERROR: {error}")

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Clean up empty 'Other' folders")
    parser.add_argument("path", help="Root path to search for 'Other' folders")
    parser.add_argument("--execute", action="store_true", help="Actually remove folders (default: dry-run)")
    args = parser.parse_args()

    root = Path(args.path).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory")
        exit(1)

    print(f"Scanning {root} for empty 'Other' folders...")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print()

    stats = clean_empty_other_folders(root, dry_run=not args.execute)

    print()
    print("Summary:")
    print(f"  Folders checked: {stats['folders_checked']}")
    print(f"  Folders removed: {stats['folders_removed']}")
    print(f"  Folders with files: {stats['folders_with_files']}")
    print(f"  Errors: {len(stats['errors'])}")

    if stats['errors']:
        print("\nErrors:")
        for error in stats['errors']:
            print(f"  - {error}")
