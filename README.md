# Organize Folder by File Type

A collision-safe Python tool for sorting folders into uppercase extension buckets such as `JPG`, `PNG`, and `MP4`.

It supports recursive modes, dry-run previews, normalization, automatic empty-folder collection into `For Deletion` by default, and an optional macOS launcher. The project began as a Hermes skill helper, but it is also useful as a standalone command-line tool.

## What it does

- Organizes files into uppercase extension folders
- Sends files without extensions to `NO_EXTENSION`
- Never overwrites files; resolves collisions with `_1`, `_2`, etc.
- Supports recursive organization (default) in two modes:
  - `flatten-root` (default): move all files into root-level buckets, then delete empty subdirectories
  - `in-place`: each directory organizes its own direct files
- Supports non-recursive organization (root files only with `--no-recursive`)
- Optionally normalizes bucket names:
  - uppercases bucket folder names
  - folds `JPEG` and `JPE` into `JPG`
- In flatten-root mode (default), empty subdirectories are removed after organization
- In non-recursive and in-place modes, collectable empty folder trees are staged into a root-level `For Deletion` folder by default
- Excludes hidden files and folders by default
- Emits structured JSON output for scripting and automation

## Requirements

- Python 3.9+
- No third-party Python dependencies

## Install with curl (one line)

Downloads the latest `main` tree from GitHub into `~/.local/share/organize-folder-by-filetype` (override with `FILE_ORG_INSTALL_DIR`):

```bash
curl -fsSL https://raw.githubusercontent.com/sethsaler/file-organization/main/scripts/install.sh | bash
```

Use a specific branch or tag (fetch `install.sh` from that ref so the script matches what you unpack):

```bash
BRANCH=main
curl -fsSL "https://raw.githubusercontent.com/sethsaler/file-organization/${BRANCH}/scripts/install.sh" | FILE_ORG_REF="$BRANCH" bash
```

After install, run the GUI (macOS or any OS with Tk):

```bash
python3 ~/.local/share/organize-folder-by-filetype/scripts/tinker_gui.py
```

## Quick start

Default (recursive flatten-root, standard normalization):

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder --normalize standard
```

Non-recursive (root files only):

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder --no-recursive
```

Recursive in-place:

```bash
python3 scripts/organize_by_filetype.py \
  --path /path/to/folder \
  --strategy in-place \
  --normalize standard
```

Dry run preview:

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder --dry-run
```

Disable automatic empty-folder collection (or deletion in flatten-root mode):

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
- `--recursive` — enable recursive organization (default)
- `--no-recursive` — disable recursive, root files only
- `--strategy {flatten-root,in-place}` — recursive strategy (default: flatten-root)
- `--include-hidden` — include hidden files and folders
- `--normalize {none,standard}` — normalization mode
- `--collect-empty-dirs` — explicitly enable empty-folder collection into `For Deletion` (default; in flatten-root mode, empty dirs are deleted instead)
- `--no-collect-empty-dirs` — disable automatic empty-folder handling
- `--dry-run` — preview changes without writing

## Behavior and safety

- Buckets are uppercase extension folders such as `JPG`, `PNG`, and `MP4`
- Alias folding maps `JPEG` and `JPE` to `JPG`
- Hidden files and folders are skipped unless explicitly included
- Existing files are never overwritten
- Name collisions are resolved by suffixing `_1`, `_2`, and so on
- In flatten-root mode (default), empty subdirectories are removed automatically after files are moved
- In non-recursive and in-place modes, empty-folder collection is enabled by default and moves collectable empty folder trees into a review bucket named `For Deletion` — folders are never deleted outright in these modes
- Use `--no-collect-empty-dirs` to disable empty-folder handling entirely
- Hidden content blocks empty-folder collection unless `--include-hidden` is explicitly enabled

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

Optional launchers are included at:

- `launchers/Organize by File Type (Tinker).command` — opens a small **Tk GUI** to pick a folder, set recursive/normalization/empty-folder options, then **Dry run** or **Run** (JSON shown in the window).
- `launchers/Organize Desktop by File Type.command` — **one-click**: organizes `~/Desktop` recursively (files only into extension folders; no overwrites; duplicate names get `_1`, `_2`, … before the extension). Does not stage empty folders into `For Deletion` so the Desktop stays predictable.
- `launchers/Organize Files by Type.command` — prompts for any folder and options, stages collectable empty folders into `For Deletion`, runs a dry-run preview first, then asks for confirmation before making changes.

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
- `scripts/tinker_gui.py` — optional Tkinter UI for exploring options and JSON output
- `scripts/install.sh` — curl-friendly installer (clone-less download from GitHub)
- `requirements-ocr.txt` — optional dependencies for the OCR script
- `launchers/Organize by File Type (Tinker).command` — double-click GUI launcher (macOS)
- `launchers/Organize Desktop by File Type.command` — one-click Desktop organizer (macOS)
- `launchers/Organize Files by Type.command` — optional macOS quick launcher for any folder
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
- `scripts/tinker_gui.py` when CLI flags or defaults change
- `launchers/Organize Files by Type.command` when relevant

## Changelog

See `CHANGELOG.md` for notable project history.

## License

MIT. See `LICENSE`.
