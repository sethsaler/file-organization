#!/usr/bin/env python3
"""Fix nested bucket folders (e.g., Images/Images -> Images)."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Set

ORGANIZER_DIR_NAME = ".organizer"

# Bucket names that might be nested
BUCKET_NAMES = {"Images", "Videos", "GIFs", "Documents", "Audio", "Archives", "Code", "Other"}


@dataclass
class ManifestEntry:
    from_path: str
    to_path: str


@dataclass
class FixManifest:
    version: int = 1
    created_at: str = ""
    base_path: str = ""
    file_moves: List[ManifestEntry] = field(default_factory=list)
    dirs_removed: List[str] = field(default_factory=list)


def write_manifest(root: Path, manifest: FixManifest) -> str:
    """Write manifest file for potential restore."""
    backup_dir = root / ORGANIZER_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = backup_dir / f"fix_nested_backup_{timestamp}.json"

    data = {
        "version": manifest.version,
        "created_at": manifest.created_at,
        "base_path": manifest.base_path,
        "file_moves": [{"from": m.from_path, "to": m.to_path} for m in manifest.file_moves],
        "dirs_removed": manifest.dirs_removed,
    }
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(manifest_path)


def restore_from_manifest(manifest_path: str) -> bool:
    """Restore files from a fix manifest."""
    manifest_file = Path(manifest_path)
    if not manifest_file.exists():
        print(f"Error: Manifest not found: {manifest_path}")
        return False

    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: Invalid manifest file: {e}")
        return False

    try:
        base = Path(data["base_path"])
    except (KeyError, TypeError):
        print("Error: Manifest missing or invalid base_path")
        return False

    if not base.exists():
        print(f"Error: Base path not found: {base}")
        return False

    # Restore files in reverse order
    restored_files = 0
    for entry in reversed(data.get("file_moves", [])):
        src, dst = base / entry["to"], base / entry["from"]
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        restored_files += 1

    # Recreate removed directories
    for rel_dir in data.get("dirs_removed", []):
        dir_path = base / rel_dir
        if not dir_path.exists():
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"Warning: Could not recreate {dir_path}: {e}")

    print(f"Restored {restored_files} files from {manifest_path}")
    return True


def fix_nested_buckets(root: Path, dry_run: bool = True) -> dict:
    """Find and fix nested bucket folders by moving contents up one level."""
    manifest = FixManifest(
        created_at=datetime.now().isoformat(),
        base_path=str(root),
    )
    stats = {
        "nested_found": 0,
        "files_moved": 0,
        "dirs_removed": 0,
        "errors": [],
        "operations": [],
        "manifest_path": None,
    }

    for parent_dir in root.rglob("*"):
        if not parent_dir.is_dir():
            continue

        # Check if this directory name is a bucket name
        if parent_dir.name not in BUCKET_NAMES:
            continue

        # Check if parent has a child with the same name (nested structure)
        nested_dir = parent_dir / parent_dir.name
        if not nested_dir.is_dir():
            continue

        stats["nested_found"] += 1
        print(f"Found nested structure: {parent_dir} -> {nested_dir}")

        # Move contents from nested to parent
        try:
            for item in nested_dir.iterdir():
                dest = parent_dir / item.name
                
                # Handle name collisions
                if dest.exists():
                    # Rename with suffix
                    stem = item.stem
                    suffix = item.suffix
                    counter = 1
                    while dest.exists():
                        new_name = f"{stem}_{counter}{suffix}" if suffix else f"{stem}_{counter}"
                        dest = parent_dir / new_name
                        counter += 1

                operation = f"Move: {item} -> {dest}"
                stats["operations"].append(operation)
                print(f"  {operation}")

                if not dry_run:
                    shutil.move(str(item), str(dest))
                    stats["files_moved"] += 1
                    # Record in manifest for potential restore
                    rel_src = str(item.relative_to(root))
                    rel_dst = str(dest.relative_to(root))
                    manifest.file_moves.append(ManifestEntry(from_path=rel_src, to_path=rel_dst))

            # Remove the now-empty nested directory
            operation = f"Remove dir: {nested_dir}"
            stats["operations"].append(operation)
            print(f"  {operation}")

            if not dry_run:
                try:
                    nested_dir.rmdir()
                    stats["dirs_removed"] += 1
                    # Record in manifest for potential restore
                    rel_nested = str(nested_dir.relative_to(root))
                    manifest.dirs_removed.append(rel_nested)
                except OSError as e:
                    error = f"Failed to remove {nested_dir}: {e}"
                    stats["errors"].append(error)
                    print(f"  ERROR: {error}")

        except Exception as e:
            error = f"Error processing {nested_dir}: {e}"
            stats["errors"].append(error)
            print(f"  ERROR: {error}")

    # Write manifest if not in dry run and there were changes
    if not dry_run and (manifest.file_moves or manifest.dirs_removed):
        try:
            manifest_path = write_manifest(root, manifest)
            stats["manifest_path"] = manifest_path
            print(f"\nManifest written to: {manifest_path}")
            print(f"To restore, run: python3 {__file__} --restore {manifest_path}")
        except Exception as e:
            error = f"Failed to write manifest: {e}"
            stats["errors"].append(error)
            print(f"ERROR: {error}")

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fix nested bucket folders")
    parser.add_argument("path", nargs="?", help="Root path to search for nested buckets (or manifest path for --restore)")
    parser.add_argument("--execute", action="store_true", help="Actually perform the moves (default: dry-run)")
    parser.add_argument("--restore", metavar="MANIFEST", help="Restore from a manifest file")
    args = parser.parse_args()

    # Handle restore mode
    if args.restore:
        if not Path(args.restore).exists():
            print(f"Error: Manifest file not found: {args.restore}")
            exit(1)
        success = restore_from_manifest(args.restore)
        exit(0 if success else 1)

    # Handle fix mode
    if not args.path:
        print("Error: path is required unless using --restore")
        parser.print_help()
        exit(1)

    root = Path(args.path).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory")
        exit(1)

    print(f"Scanning {root} for nested bucket structures...")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print()

    stats = fix_nested_buckets(root, dry_run=not args.execute)

    print()
    print("Summary:")
    print(f"  Nested structures found: {stats['nested_found']}")
    print(f"  Files moved: {stats['files_moved']}")
    print(f"  Directories removed: {stats['dirs_removed']}")
    print(f"  Errors: {len(stats['errors'])}")
    if stats['manifest_path']:
        print(f"  Manifest: {stats['manifest_path']}")

    if stats['errors']:
        print("\nErrors:")
        for error in stats['errors']:
            print(f"  - {error}")
