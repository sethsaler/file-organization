# Organize Folder by File Type

A collision-safe file organizer that sorts files into uppercase extension buckets such as `JPG`, `PNG`, and `MP4`.

This project started as a Hermes skill helper and is useful on its own as a standalone script.

## Features

- Organizes files into uppercase extension folders
- Sends files without extensions to `NO_EXTENSION`
- Never overwrites files; resolves collisions with `_1`, `_2`, etc.
- Supports non-recursive organization
- Supports recursive organization in two modes:
  - `in-place`: each directory organizes its own direct files
  - `flatten-root`: move all files into root-level buckets
- Optional normalization mode:
  - uppercases bucket folder names
  - folds `JPEG` and `JPE` into `JPG`
- Excludes hidden files and folders by default
- Emits structured JSON output for easy automation

## Files

- `scripts/organize_by_filetype.py` — main Python helper
- `SKILL.md` — original Hermes skill documentation
- `launchers/Organize Files by Type.command` — optional macOS quick launcher

## Requirements

- Python 3.9+

## Usage

Non-recursive:

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder
```

Recursive in-place:

```bash
python3 scripts/organize_by_filetype.py \
  --path /path/to/folder \
  --recursive \
  --strategy in-place \
  --normalize standard
```

Recursive flatten-to-root:

```bash
python3 scripts/organize_by_filetype.py \
  --path /path/to/folder \
  --recursive \
  --strategy flatten-root \
  --normalize standard
```

Dry run:

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder --dry-run
```

Include hidden files:

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder --include-hidden
```

## CLI arguments

- `--path PATH` — target directory
- `--recursive` — enable recursive organization
- `--strategy {in-place,flatten-root}` — recursive strategy
- `--include-hidden` — include hidden files and folders
- `--normalize {none,standard}` — normalization mode
- `--dry-run` — preview changes without writing

## JSON output

The script prints a JSON summary including:

- target path
- mode and strategy
- files moved
- moves by extension
- collision count
- folders touched
- normalization stats
- verification summary

## Notes

- `flatten-root` is intentionally more destructive to directory layout than `in-place`; use it only when you want full consolidation.
- Hidden files and folders are skipped unless explicitly included.
- Normalization is especially useful after recursive runs.
