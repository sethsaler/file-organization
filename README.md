# Organize Folder by File Type

A collision-safe Python tool for sorting files into **Images**, **Videos**, **GIFs**, and **Other** at the folder you choose (including everything nested under it when recursive). Unknown or extensionless files go to **Other**.

It supports recursive modes, dry-run previews, normalization, automatic empty-folder collection into `For Deletion` by default, and an optional macOS launcher. The project began as a Hermes skill helper, but it is also useful as a standalone command-line tool.

## What it does

- Organizes files into `Images`, `Videos`, `GIFs`, and `Other` (animated GIFs use `.gif` and go to **GIFs**, not Images)
- Never overwrites files; resolves collisions with `_1`, `_2`, etc.
- Supports recursive organization (default) in two modes:
  - `flatten-root` (default): move all files from **any depth** into folders directly under the target path (`Images`, `Videos`, `GIFs`, `Other` by default). Traversal skips only `For Deletion` and `.organizer`, so **legacy extension folders at the root** (for example a prior `JPG/` or `PNG/` sort) are still fully scanned.
  - `in-place`: each directory organizes its own direct files
- Supports non-recursive organization (root files only with `--no-recursive`)
- In scheduled mode, supports `expand_subfolders` to organize each immediate subdirectory independently instead of treating the parent as a single recursive target
- Optionally normalizes bucket names (`--normalize standard`): canonical casing for `Images`, `Videos`, `GIFs`, `Other`
- In flatten-root mode (default), collectable empty folder trees are staged into `For Deletion` first (multi-round), then any remaining empty directories (including empty bucket folders) are removed
- In non-recursive and in-place modes, collectable empty folder trees are staged into a root-level `For Deletion` folder by default; a final pass removes leftover empties
- Includes hidden files and folders by default; use `--no-include-hidden` to skip dotfiles
- Optional duplicate detection (`--detect-duplicates`): identical-content files are staged into a root-level `Duplicates` folder instead of their bucket (size-first grouping with lazy hashing, so unique files are never read)
- Scheduled folders can run daily, on an interval, or in **watch** mode (organize shortly after files change)
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

After install, run the folder-picker GUI (macOS or any OS with Tk):

```bash
python3 ~/.local/share/organize-folder-by-filetype/scripts/tinker_gui.py
```

The GUI opens as a command center:

- **Overview** shows watched folders, recent results, safety notices, and the main actions.
- **Organize once** requires a successful preview before the organize action is enabled. The preview lists planned source and destination paths without changing files.
- **Watched folders** keeps the folder list focused; per-folder and scheduler tuning open only when requested.
- **History** shows results and enables Undo while a recovery manifest remains available.
- **Advanced settings** contains the standalone random-rename tool and technical logs. Random naming is opt-in.

Use **Watched folders** (or the standalone `schedule_gui.py`) to auto-organize folders. Config is saved to `~/.config/file-organization/schedule.json`.

1. In **Organize once**, preview a folder and choose **Watch this folder…**, or use **Add folder…** under **Watched folders**.
2. Open **Scheduler settings…** to run once daily, every *N* minutes, or shortly after watched files change.
3. Open a folder's settings to organize immediate subfolders independently or set a minimum unsorted-file threshold.
4. Enable automatic runs — a background scheduler keeps running after you close the app (LaunchAgent on macOS, systemd user unit on Linux).

The GUI installs and controls the background daemon automatically. Advanced setups can still run `schedule_daemon.py` manually:

```bash
python3 ~/.local/share/organize-folder-by-filetype/scripts/schedule_daemon.py --foreground
```

For **cron** at midnight (one batch per invocation; honors `scheduler_enabled` unless you pass `--force`):

```cron
0 0 * * * /usr/bin/python3 /FULL/PATH/TO/scripts/schedule_daemon.py --once >>"$HOME/.cache/file-org-scheduler.log" 2>&1
```

Example `schedule.json` for a single folder every night at midnight:

```json
{
  "version": 5,
  "schedule_mode": "daily",
  "daily_time": "00:00",
  "scheduler_enabled": true,
  "folders": [
    {
      "path": "/path/to/your/folder",
      "enabled": true,
      "dry_run_verified": true
    }
  ]
}
```

To organize each subfolder of a parent directory independently, set `"expand_subfolders": true`:

```json
{
  "version": 5,
  "schedule_mode": "daily",
  "daily_time": "00:00",
  "scheduler_enabled": true,
  "folders": [
    {
      "path": "/path/to/parent/folder",
      "enabled": true,
      "recursive": true,
      "expand_subfolders": true,
      "dry_run_verified": true
    }
  ]
}
```

Set `max_parallel` in the JSON: **0** means all enabled folders at once (capped at 32); a positive number limits concurrent runs. Example unit files: `install/systemd/`, `install/launchd/`.

### Watch mode (near real-time)

Set `"schedule_mode": "watch"` to organize a folder shortly after files land in it instead of waiting for a timer:

```json
{
  "version": 5,
  "schedule_mode": "watch",
  "scheduler_enabled": true,
  "folders": [
    {
      "path": "/path/to/your/folder",
      "enabled": true,
      "dry_run_verified": true
    }
  ]
}
```

- **Native FS events (recommended):** install the optional [`watchdog`](https://pypi.org/project/watchdog/) package (`python3 -m pip install --user watchdog`, or `pip install "organize-folder-by-filetype[watch]"`) and the watch daemon reacts to filesystem events (FSEvents on macOS, inotify on Linux) at **any depth** under each watched folder within milliseconds. A relaxed full recursive scan still runs every 60 s as a safety net in case an event is ever dropped. The `curl | bash` installer installs watchdog automatically (best-effort).
- **Polling fallback (no watchdog):** the daemon uses tiered polling. A fast signature (`stat` of the watched root and its immediate subdirectories) runs every `watch_poll_seconds` (default 0.25 s); a full recursive scan runs every 5 s to catch changes deeper than one level.
- After a change is detected, the folder must stay quiet for `watch_quiet_seconds` (default 0.3 s) before organizing — in-progress copies keep resetting the timer, so large files finish before being moved. Each folder gets its own background worker, so a long-running folder does not block the watcher from organizing other folders. Timings are configurable in `schedule.json` (`watch_poll_seconds`, `watch_quiet_seconds`) or in the Schedule tab.
- **macOS (LaunchAgent):** enabling automatic runs in watch mode installs a persistent agent running the watch daemon (`--foreground`, `RunAtLoad` + `KeepAlive`). Watched folders are read from `schedule.json` on the daemon's periodic config reload, so adding/removing folders needs no reinstall.
- Combine with `min_unsorted_threshold` to only fire once enough files have accumulated.
- The daemon logs the active backend at startup (`"backend": "fsevents" | "inotify" | "polling"`) in `~/.local/state/file-organization/schedule-daemon.log`.

### Duplicate detection

Add `--detect-duplicates` (CLI), enable "Detect identical duplicates" in the command center, or set `"detect_duplicates": true` on a folder job. During organization each file's size is indexed and, only when two files share a size, their contents are hashed (BLAKE2). Later copies of identical content are staged into a root-level `Duplicates` folder instead of their bucket — nothing is ever deleted, and the moves are recorded in the backup manifest so `--restore` undoes them. Files already inside root bucket folders count as the canonical copies, so re-running against a folder keeps the organized file and stages the new arrival.

### Threshold-gated runs

Set `min_unsorted_threshold` on a folder to skip the run when fewer than N unsorted files have accumulated. The scheduler counts loose files before running — for `flatten-root`, this is files directly at the folder root; for `in-place`, it walks recursively (skipping bucket dirs, `For Deletion`, `.organizer`). Useful with short interval polling so the organizer only fires when there's actual work to do:

```json
{
  "version": 5,
  "schedule_mode": "interval",
  "interval_minutes": 30,
  "scheduler_enabled": true,
  "folders": [
    {
      "path": "/path/to/folder",
      "enabled": true,
      "dry_run_verified": true,
      "min_unsorted_threshold": 20
    }
  ]
}
```

With the above config, launchd fires `--once` every 30 minutes; the scheduler checks the unsorted count and only runs the organizer when ≥ 20 loose files are present. **Watched folders → Edit selected…** sets this threshold per folder.

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

Disable automatic empty-folder staging into `For Deletion` (flatten-root will only remove empties on disk):

```bash
python3 scripts/organize_by_filetype.py \
  --path /path/to/folder \
  --no-collect-empty-dirs
```

Skip hidden files (default is to include them):

```bash
python3 scripts/organize_by_filetype.py --path /path/to/folder --no-include-hidden
```

## CLI arguments

- `--path PATH` — target directory
- `--recursive` — enable recursive organization (default)
- `--no-recursive` — disable recursive, root files only
- `--strategy {flatten-root,in-place}` — recursive strategy (default: flatten-root)
- `--include-hidden` — include hidden files and folders (default behavior)
- `--no-include-hidden` — exclude dotfiles and dot-directories
- `--normalize {none,standard}` — normalization (default: **standard** when recursive, **none** when not)
- `--profile {standard,extended,FILE.json}` — bucket set (`extended` adds Documents, Audio, Archives, Code)
- `--exclude PATTERN` — skip matching directory names or globs (repeatable)
- `--exclude-defaults` — also skip `.git`, `node_modules`, `venv`, `__pycache__`, etc.
- `--no-follow-symlinks` — do not follow directory symlinks when walking
- `--mime-sniff` — classify extensionless files from file headers
- `--detect-duplicates` — stage identical-content copies into `Duplicates` instead of their bucket
- `--duplicates-hardlink` — with `--detect-duplicates`, keep duplicates in their bucket as hardlinks to the canonical copy (no extra disk space)
- `--date-buckets` — place files under `Bucket/YYYY/MM` subfolders by modification time
- `--random-names` — replace moved filenames with random unique names (opt-in)
- `--no-random-names` — keep original filenames (default)
- `--verbose` — progress on stderr during large runs
- `--ocr-index` — after organizing, OCR PNG/JPEG under `Images/` into `.organizer/ocr_index.csv` (needs OCR deps)
- `--collect-empty-dirs` / `--no-collect-empty-dirs` — empty-folder staging (default: on)
- `--dry-run` — preview changes without writing
- `--restore MANIFEST` — undo a run from `.organizer/backup_*.json`
- `--json-out FILE` — write JSON summary to a file

Restore without the main CLI:

```bash
python3 scripts/restore_from_backup.py /path/to/folder/.organizer/backup_20260101_120000.json
python3 scripts/restore_from_backup.py --list /path/to/folder
```

## Behavior and safety

- **Buckets:** `Images`, `Videos`, `GIFs`, `Other` by default; use `--profile extended` for more types
- **Dotfiles:** included by default (e.g. `.DS_Store` moves with other files unless `--no-include-hidden`)
- Hidden files and folders are organized like visible ones unless `--no-include-hidden` is set
- Existing files are never overwritten
- Name collisions are resolved by suffixing `_1`, `_2`, and so on
- In flatten-root mode (default), empty trees are staged into `For Deletion` when collection is enabled; afterward, remaining empty directories are removed (including unused bucket folders)
- When `--no-collect-empty-dirs` is set, empty folders are removed in place only (nothing moved to `For Deletion`)
- In non-recursive and in-place modes, behavior matches: stage collectable trees to `For Deletion` by default, then trim leftover empties
- Use `--no-collect-empty-dirs` to disable staging entirely

## JSON output

The script prints a JSON summary including:

- target path
- mode and strategy
- `buckets` (always Images / Videos / GIFs / Other)
- files moved
- `moved_by_category` — counts per bucket folder
- collision count
- `duplicates` — whether detection ran, how many copies were staged, sample moves
- folders touched
- normalization stats
- empty-folder collection stats
- verification summary

## macOS launcher

Optional launchers are included at:

- `launchers/Organize by File Type (Tinker).command` — opens a small **Tk GUI** to pick a folder, set recursive/normalization/empty-folder options, then **Dry run** or **Run** (JSON shown in the window).
- `launchers/Organize Desktop by File Type.command` — **one-click**: organizes `~/Desktop` recursively into **Images / Videos / GIFs / Other** (flatten-root, standard normalization; empty trees staged to `For Deletion` by default).
- `launchers/Organize Files by Type.command` — prompts for a folder: same as Desktop (flatten-root, standard normalization, `For Deletion` staging, dry-run preview then confirm).

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
- `scripts/schedule_config.py` — shared `schedule.json` schema and parallel runs
- `scripts/schedule_gui.py` — schedule editor (Tk)
- `scripts/schedule_daemon.py` — background or `--once` (cron) runner
- `install/systemd/`, `install/launchd/` — example service files
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
- `scripts/schedule_config.py`, `scripts/schedule_gui.py`, `scripts/schedule_daemon.py` when schedule JSON or runner behavior changes
- `launchers/Organize Files by Type.command` when relevant

## Changelog

See `CHANGELOG.md` for notable project history.

## License

MIT. See `LICENSE`.
