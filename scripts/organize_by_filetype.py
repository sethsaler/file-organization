#!/usr/bin/env python3
"""CLI entry point for folder organization by file type."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_buckets import PROFILE_EXTENDED, PROFILE_STANDARD, resolve_profile
from org_exclude import merge_exclude_patterns
from org_manifest import restore_from_manifest
from org_organizer import Organizer
from org_paths import normalize_folder_input
from org_rules import VALID_FALLBACKS, RuleSet, load_archive_recipe, load_rule_set


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Organize files into type buckets at the target root "
            "(recursive modes, optional normalization, empty-folder staging)."
        )
    )
    parser.add_argument("--path", help="Target directory path")
    parser.add_argument("--recursive", action="store_true", default=True, help="Enable recursive organization (default)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Disable recursive, root files only")
    parser.add_argument(
        "--strategy",
        choices=["in-place", "flatten-root"],
        default="flatten-root",
        help="Recursive strategy (ignored when not recursive)",
    )
    hidden_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(include_hidden=True)
    hidden_group.add_argument("--include-hidden", dest="include_hidden", action="store_true", help="Include hidden files (default)")
    hidden_group.add_argument("--no-include-hidden", dest="include_hidden", action="store_false", help="Exclude dotfiles")
    parser.add_argument(
        "--normalize",
        choices=["none", "standard"],
        default=None,
        help="Normalization mode (default: standard when recursive, none otherwise)",
    )
    empty_dir_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(collect_empty_dirs=True)
    empty_dir_group.add_argument("--collect-empty-dirs", dest="collect_empty_dirs", action="store_true", help="Stage empty folders to For Deletion (default)")
    empty_dir_group.add_argument("--no-collect-empty-dirs", dest="collect_empty_dirs", action="store_false", help="Skip empty-folder staging")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    backup_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(create_backup=True)
    backup_group.add_argument("--backup", dest="create_backup", action="store_true", help="Create backup manifest (default)")
    backup_group.add_argument("--no-backup", dest="create_backup", action="store_false", help="Skip backup manifest")
    parser.add_argument("--restore", metavar="MANIFEST", help="Restore from a backup manifest")
    parser.add_argument(
        "--profile",
        default=PROFILE_STANDARD,
        metavar="NAME|FILE",
        help=f"Bucket profile: {PROFILE_STANDARD}, {PROFILE_EXTENDED}, or path to JSON",
    )
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN", help="Exclude dir name or glob (repeatable)")
    parser.add_argument("--exclude-defaults", action="store_true", help="Also exclude .git, node_modules, venv, etc.")
    parser.add_argument("--no-follow-symlinks", action="store_true", help="Do not follow directory symlinks when walking")
    parser.add_argument("--mime-sniff", action="store_true", help="Sniff extensionless files by content")
    parser.add_argument("--verbose", action="store_true", help="Progress messages on stderr")
    parser.add_argument("--ocr-index", action="store_true", help="After run, OCR PNG/JPEG in Images/ to .organizer/ocr_index.csv")
    parser.add_argument("--json-out", metavar="FILE", help="Write JSON summary to file instead of stdout only")
    random_names_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(random_names=False)
    random_names_group.add_argument("--random-names", dest="random_names", action="store_true", help="Rename moved files with random unique sequences (preserves extensions)")
    random_names_group.add_argument("--no-random-names", dest="random_names", action="store_false", help="Keep original filenames (default)")
    parser.add_argument("--random-names-after-organize", action="store_true", help="After organizing, rename all files with random unique sequences")
    skip_random_group = parser.add_mutually_exclusive_group()
    parser.set_defaults(skip_randomly_renamed=True)
    skip_random_group.add_argument("--skip-randomly-renamed", dest="skip_randomly_renamed", action="store_true", help="Skip files that appear to be already randomly renamed (16-char alphanumeric filenames) (default)")
    skip_random_group.add_argument("--no-skip-randomly-renamed", dest="skip_randomly_renamed", action="store_false", help="Do not skip files that appear to be already randomly renamed")
    parser.add_argument(
        "--detect-duplicates",
        action="store_true",
        help="Detect files with identical content (size + hash) and stage the copies into a Duplicates folder instead of the bucket",
    )
    parser.add_argument(
        "--duplicates-hardlink",
        action="store_true",
        help="With --detect-duplicates: keep duplicates in their bucket as hardlinks to the canonical copy (no extra disk space) instead of staging them into Duplicates",
    )
    parser.add_argument(
        "--date-buckets",
        action="store_true",
        help="Place files under Bucket/YYYY/MM subfolders based on modification time",
    )
    parser.add_argument(
        "--rules",
        metavar="FILE",
        help="Ordered JSON routing rules; the first matching rule chooses the destination",
    )
    parser.add_argument(
        "--unmatched",
        choices=sorted(VALID_FALLBACKS),
        default=None,
        help="What to do when no routing rule matches: bucket, needs-review, or leave",
    )
    parser.add_argument(
        "--archive-root",
        metavar="DIR",
        help="Use the Downloader archive recipe and route into this explicit archive root",
    )
    parser.add_argument(
        "--archive-mapping",
        metavar="FILE",
        help="JSON map of Downloader source folder names to archive-relative destinations",
    )
    return parser.parse_args()


def _progress(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()


def main() -> None:
    args = parse_args()

    if args.restore:
        if not restore_from_manifest(args.restore):
            sys.exit(1)
        return

    if not args.path:
        print(json.dumps({"error": "--path is required unless using --restore"}, indent=2))
        sys.exit(2)

    base = normalize_folder_input(args.path).resolve()
    if not base.exists() or not base.is_dir():
        print(json.dumps({"error": f"Path not found or not a directory: {base}"}, indent=2))
        sys.exit(1)

    normalize = args.normalize
    if normalize is None:
        normalize = "standard" if args.recursive else "none"

    try:
        profile_label, profile_buckets = resolve_profile(args.profile)
    except ValueError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        sys.exit(2)

    excludes = merge_exclude_patterns(args.exclude or [], use_defaults=args.exclude_defaults)

    if args.rules and args.archive_root:
        print(json.dumps({"error": "Use --rules or --archive-root, not both"}, indent=2))
        sys.exit(2)
    if args.archive_mapping and not args.archive_root:
        print(json.dumps({"error": "--archive-mapping requires --archive-root"}, indent=2))
        sys.exit(2)
    try:
        rule_set = load_rule_set(Path(args.rules)) if args.rules else None
        if args.unmatched:
            if rule_set is None:
                rule_set = RuleSet(unmatched=args.unmatched)
            else:
                rule_set.unmatched = args.unmatched
        archive_recipe = (
            load_archive_recipe(
                Path(args.archive_root),
                Path(args.archive_mapping) if args.archive_mapping else None,
            )
            if args.archive_root
            else None
        )
    except ValueError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        sys.exit(2)

    org = Organizer(
        base=base,
        recursive=args.recursive,
        strategy=args.strategy,
        include_hidden=args.include_hidden,
        normalize=normalize,
        collect_empty_dirs=args.collect_empty_dirs,
        dry_run=args.dry_run,
        create_backup=args.create_backup,
        profile_label=profile_label,
        profile_buckets=profile_buckets,
        exclude_patterns=excludes,
        follow_symlinks=not args.no_follow_symlinks,
        use_mime_sniff=args.mime_sniff,
        verbose=args.verbose,
        ocr_index=args.ocr_index,
        progress_callback=_progress if args.verbose else None,
        random_names=args.random_names,
        random_names_after_organize=args.random_names_after_organize,
        skip_randomly_renamed=args.skip_randomly_renamed,
        detect_duplicates=args.detect_duplicates,
        duplicates_hardlink=args.duplicates_hardlink,
        date_buckets=args.date_buckets,
        rule_set=rule_set,
        archive_recipe=archive_recipe,
    )
    result = org.run()
    out = json.dumps(result, indent=2)
    print(out)
    if args.json_out:
        Path(args.json_out).write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
