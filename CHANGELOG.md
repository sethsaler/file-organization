# Changelog

All notable changes to this project will be documented in this file.

This project tracks changes using the Hermes skill version as its public version number.

## [Unreleased]

- Ongoing improvements, cleanup, and repository polish.

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
