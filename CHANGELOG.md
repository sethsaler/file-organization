# Changelog

All notable changes to this project will be documented in this file.

This project tracks changes using the Hermes skill version as its public version number.

## [1.8.0] - 2026-05-23

### Added

- Modular helpers: `org_buckets.py`, `org_exclude.py`, `org_mime.py`, `org_manifest.py`, `org_organizer.py`, `org_logging.py`.
- `pytest` suite and GitHub Actions CI (`.github/workflows/test.yml`).
- CLI: `--profile standard|extended|file.json`, `--exclude`, `--exclude-defaults`, `--no-follow-symlinks`, `--mime-sniff`, `--verbose`, `--ocr-index`, `--json-out`.
- `scripts/restore_from_backup.py` and `restore_*.sh` for cross-platform rollback.
- Install adds `organize-by-filetype` and `restore-file-organization` to `~/.local/bin`.
- Scheduler: file logging (`~/.local/state/file-organization/scheduler.log`), path overlap warnings, auto-disable after repeated failures, dry-run preview when adding folders in `schedule_gui.py`.
- Tinker GUI: profile, exclude defaults, verbose/MIME options, restore from latest manifest.

### Changed

- Recursive runs default to `--normalize standard` when not specified.
- `schedule.json` schema version 3 (`profile`, `exclude`, failure counters).

## [Unreleased]

### Changed

- **Removed per-extension mode:** `--by-extension` is gone. Output JSON uses `moved_by_category`, `buckets`, and `noncanonical_bucket_dirs_*`; `alias_map` dropped from normalization stats.

- **Traversal / legacy buckets:** Directory walking no longer skips folders whose names look like extension or category buckets (e.g. root-level `JPG/` or `Images/`). Only `For Deletion` and `.organizer` are skipped, so trees like iCloud `PreSorted/JPG/` are scanned. **Idempotent moves:** files already inside the correct destination folder are not moved again.

- **Flatten-root + empty folders:** With default `--collect-empty-dirs`, flatten-root no longer deletes empty trees before collection; collectable trees are moved to `For Deletion` in multiple rounds, then a final pass removes leftover empty dirs (including empty bucket folders). Repo launchers and Desktop helper now use staging for flatten as well. `--no-collect-empty-dirs` still skips staging.

- **Category-only buckets:** Default organization is **Images**, **Videos**, **GIFs**, and **Other** (GIFs separate from still images). Tinker no longer offers an extension-folder toggle.

### Added

- `scripts/install.sh`: curl-friendly installer that unpacks a GitHub ref into `~/.local/share/organize-folder-by-filetype` (configurable via `FILE_ORG_*` env vars).
- `scripts/tinker_gui.py`: Tkinter UI to configure options and run dry-run or live organize with JSON output.
- `launchers/Organize by File Type (Tinker).command`: macOS launcher for the tinker GUI.

## [1.5.1] - 2026-03-30

### Added

- `launchers/Organize Desktop by File Type.command`: one-click organization of `~/Desktop` by file type (recursive in-place, standard normalization, collision-safe renames, no empty-folder staging into `For Deletion`).

## [1.5.0] - 2026-03-13

### Changed

- Empty-folder collection into `For Deletion` is now enabled by default.
- Added `--no-collect-empty-dirs` as the explicit opt-out for CLI usage.
- Simplified the macOS launcher so empty-folder staging happens automatically without an extra prompt.
- Updated `README.md` and `SKILL.md` to describe the new default behavior.

## [1.4.0] - 2026-03-13

### Added

- Optional `--collect-empty-dirs` mode to move collectable empty folder trees into a root-level `For Deletion` folder.
- Empty-folder collection reporting in the JSON summary, including collision counts and sample moves.
- Launcher prompt for sending empty folders to `For Deletion`.

### Changed

- Recursive traversal now skips the `For Deletion` review folder so repeated runs leave quarantined empties alone.
- Updated `README.md` and `SKILL.md` to document empty-folder collection workflow and safety rules.

## [1.3.1] - 2026-03-12

### Added

- MIT `LICENSE` file.
- `CHANGELOG.md` for tracked project history.

### Changed

- Improved `README.md` for GitHub-facing documentation.
- Clarified that the GitHub working copy is the canonical source for the Hermes skill.
- Updated `SKILL.md` to distinguish repository-facing docs from agent-facing instructions.

## [1.3.0] - 2026-03-10

### Added

- Standalone helper script at `scripts/organize_by_filetype.py`.
- Structured JSON output for reporting and automation.
- Support for non-recursive organization.
- Support for recursive organization with `in-place` and `flatten-root` strategies.
- Dry-run support.
- Collision-safe naming for moved files.
- Optional macOS launcher at `launchers/Organize Files by Type.command`.

### Changed

- Optimized the workflow around a single reusable helper script.
- Improved normalization behavior for alias merging and canonical bucket handling.

### Fixed

- Quick-launch path parsing for iCloud-style escaped paths.
- Case-insensitive filesystem handling for normalization and case-only renames.

## [1.2.0] - 2026-03-10

### Added

- Standard normalization mode.

### Changed

- Canonical uppercase bucket names for extension folders.
- Alias folding from `JPEG` and `JPE` into `JPG`.

### Fixed

- Safer normalization behavior on case-insensitive filesystems.
