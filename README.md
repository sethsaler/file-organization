# Organize Folder by File Type

A collision-safe Python tool for sorting folders into uppercase extension buckets such as `JPG`, `PNG`, and `MP4`.

It supports recursive modes, dry-run previews, normalization, automatic empty-folder collection into `For Deletion` by default, and an optional macOS launcher. The project began as a Hermes skill helper, but it is also useful as a standalone command-line tool.

## What it does

- Organizes files into uppercase extension folders
- Sends files without extensions to `NO_EXTENSION`
- Never overwrites files; resolves collisions with `_1`, `_2`, etc.
- Supports non-recursive organization
- Supports recursive organization in two modes:
  - `in-place`: each directory organizes its own direct files
  - `flatten-root`: move all files into root-level buckets
- Optionally normalizes bucket names:
  - uppercases bucket folder names
  - folds `JPEG` and `JPE` into `JPG`
- Automatically moves collectable empty folder trees into a root-level `For Deletion` folder by default
- Excludes hidden files and folders by default
- Emits structured JSON output for scripting and automation

## Requirements

- Python 3.9+
- No third-party Python dependencies

## Quick start

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

Dry run preview:

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder --dry-run
```

Disable automatic empty-folder collection:

```bash
python3 scripts/organize_by_filetype.py \
  --path /path/to/folder \
  --no-collect-empty-dirs
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
- `--collect-empty-dirs` — explicitly enable empty-folder collection into `For Deletion` (already the default)
- `--no-collect-empty-dirs` — disable automatic empty-folder collection
- `--dry-run` — preview changes without writing

## Behavior and safety

- Buckets are uppercase extension folders such as `JPG`, `PNG`, and `MP4`
- Alias folding maps `JPEG` and `JPE` to `JPG`
- Hidden files and folders are skipped unless explicitly included
- Existing files are never overwritten
- Name collisions are resolved by suffixing `_1`, `_2`, and so on
- Empty-folder collection is enabled by default and never deletes folders; it only moves collectable empty folder trees into a review bucket named `For Deletion`
- Use `--no-collect-empty-dirs` to disable that automatic staging behavior
- Hidden content blocks empty-folder collection unless `--include-hidden` is explicitly enabled
- `flatten-root` intentionally consolidates the directory tree and should only be used when that behavior is desired

## JSON output

The script prints a JSON summary including:

- target path
- mode and strategy
- files moved
- moves by extension
- collision count
- folders touched
- normalization stats
- empty-folder collection stats
- verification summary

## macOS launcher

An optional launcher is included at:

- `launchers/Organize Files by Type.command`

It prompts for the target folder and core options, automatically stages collectable empty folders into `For Deletion`, runs a dry run preview first, and then asks for confirmation before making changes.

## Image text extraction (OCR)

`scripts/extract_image_text.py` reads PNG and JPEG images, runs [Tesseract](https://github.com/tesseract-ocr/tesseract) OCR, and writes a spreadsheet with columns `file_name` and `extracted_text` (CSV or Excel).

### Requirements

- Python 3.9+
- The Tesseract OCR engine on your PATH (for example `apt install tesseract-ocr` on Debian/Ubuntu, or `brew install tesseract` on macOS)
- Python packages: `pip install -r requirements-ocr.txt` (or install `pytesseract`, `Pillow`, and `openpyxl` yourself)

### Examples

Single image to CSV (default output: `ocr_results.csv` next to the image):

```bash
python3 scripts/extract_image_text.py /path/to/scan.png
```

Folder of images, recursive, Excel output:

```bash
python3 scripts/extract_image_text.py /path/to/folder \
  --recursive \
  --format excel \
  -o /path/to/results.xlsx
```

Optional `--lang eng+deu` passes Tesseract language packs. Use `--include-errors` to add an `error` column when a file fails.

## Repository layout

- `scripts/organize_by_filetype.py` — main Python helper
- `scripts/extract_image_text.py` — OCR helper: image text to CSV/Excel
- `requirements-ocr.txt` — optional dependencies for the OCR script
- `launchers/Organize Files by Type.command` — optional macOS quick launcher
- `SKILL.md` — Hermes skill instructions
- `README.md` — repository-facing documentation
- `CHANGELOG.md` — notable project history
- `LICENSE` — repository license

## Using with Hermes

This repository is intended to be the canonical source for the Hermes skill as it evolves.

A Hermes install can use this repository directly by linking or copying these files into a skill directory such as:

```text
~/.hermes/skills/productivity/organize-folder-by-filetype/
```

At minimum, Hermes needs:

- `SKILL.md`
- `scripts/organize_by_filetype.py`

If behavior changes, keep the following in sync:

- `scripts/organize_by_filetype.py`
- `SKILL.md`
- `README.md`
- `launchers/Organize Files by Type.command` when relevant

## Changelog

See `CHANGELOG.md` for notable project history.

## License

MIT. See `LICENSE`.
