# Changelog

All notable changes to this project will be documented in this file.

This project tracks changes using the Hermes skill version as its public version number.

## [Unreleased]

- Ongoing improvements, cleanup, and repository polish.

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
