---
name: organize-folder-by-filetype
description: Efficient file-type organization with a single optimized Python helper (non-recursive/recursive, optional normalization, dry-run, collision-safe moves, and automatic empty-folder collection).
version: 1.5.1
metadata:
  hermes:
    tags: [filesystem, organization, cleanup, file-management, optimization]
    related_skills: []
---

# Organize Folder by File Type (Optimized)

## When to use

Use this skill when a user wants a folder or folder tree reorganized into extension buckets like JPG, PNG, and MP4.

## Canonical source

Treat the repository working copy as the canonical source for this skill when one exists.

Keep changes synchronized across:

- `scripts/organize_by_filetype.py`
- `SKILL.md`
- `README.md`
- `launchers/Organize Files by Type.command` when launcher behavior is affected
- `scripts/tinker_gui.py` when CLI flags or GUI options should stay aligned

Use `README.md` for repository-facing documentation and `SKILL.md` for agent-facing operating instructions.

## Core behavior

- Buckets are uppercase extension folders (for example jpg -> JPG).
- Files without extension go to NO_EXTENSION.
- No overwrite ever; collisions are suffixed (_1, _2, ...).
- Hidden files and folders are included by default (`--no-include-hidden` to exclude dotfiles).
- In flatten-root mode (default), empty subdirectories are automatically removed after files are moved to root-level buckets.
- In non-recursive and in-place modes, automatic empty-folder collection moves collectable empty folder trees into a root-level `For Deletion` review folder by default.

## Modes

- Non-recursive: only target folder direct files.
- `flatten-root`: every file under the tree (any depth) moves into extension buckets **directly under the chosen folder**. Traversal skips only **root-level** bucket/`For Deletion` dirs so nested folders named like extensions (e.g. `project/JPG/`) are still scanned.

## Normalization

- none: skip normalization.
- standard:
  - canonical uppercase bucket names (for example WebP -> WEBP)
  - alias folds:
    - JPEG -> JPG
    - JPE -> JPG

Default recommendation:
- Recursive runs: use standard normalization.
- Non-recursive runs: normalization optional.

## Efficiency design

This skill uses one reusable helper script at `scripts/organize_by_filetype.py` to reduce tool chatter and repeated scans.

Performance characteristics:
- single command execution for main operation
- O(N) directory walk for movement stage
- top-down pruning of known bucket folders (prevents redundant traversal)
- optional bottom-up normalization pass only when requested
- structured JSON output for direct reporting
- dry-run mode for fast planning and validation without writes

## Project structure

- `scripts/organize_by_filetype.py` — main helper
- `scripts/tinker_gui.py` — optional Tk UI for folder pick, flags, dry-run/run, JSON output
- `scripts/install.sh` — one-line curl installer (GitHub tarball into a chosen directory)
- `launchers/Organize by File Type (Tinker).command` — macOS double-click for the Tk UI
- `launchers/Organize Desktop by File Type.command` — one-click `~/Desktop` run (recursive flatten-root, standard normalization, `--no-collect-empty-dirs`)
- `launchers/Organize Files by Type.command` — prompts for a folder, then flatten-root + standard normalization + empty-folder deletion (dry-run preview, then confirm)
- `README.md` — repository-facing documentation
- `SKILL.md` — agent-facing skill instructions

## Execution workflow

1) Confirm user inputs
- target path
- For CLI/Tinker: recursive vs non-recursive, strategy, normalization, hidden, empty-folder handling, dry-run as needed
- For `Organize Files by Type.command`: only the folder path (behavior is fixed: recursive flatten-root, standard normalization, delete empty dirs, dry-run then confirm)

2) Run helper script
- Script location: `scripts/organize_by_filetype.py`
- Use terminal to run the Python script with the requested flags.
- Prefer one call per job (optionally a dry-run first, then real run).

3) Parse JSON output and report
Always report:
- target
- mode and strategy
- normalization mode
- files moved total
- counts by extension
- collisions resolved
- folders touched
- normalization stats
- empty-folder collection / removal stats
- verification summary (root remaining files, noncanonical dirs)

## Required safety rules

- Never delete user files.
- Never overwrite files.
- Flatten-root is the default mode; files are consolidated into root-level buckets and empty subdirectories are removed.
- Preserve hierarchy in recursive in-place mode.
- Empty-folder collection is on by default for non-recursive and in-place modes, and must move folders into `For Deletion`, not remove them outright.
- In flatten-root mode, empty subdirectories are removed directly (not staged into `For Deletion`).
- Treat case-only folder normalization safely on case-insensitive filesystems (temporary rename sequence).

## Notes

- For very large trees, do a dry run first to estimate scope.
- Use `--no-include-hidden` when the user wants dotfiles and dot-directories left alone.
- If the user asks to normalize aliases or casing after organization, run with `--normalize standard`.
- For non-recursive and in-place modes, empty-folder staging into `For Deletion` is the default unless `--no-collect-empty-dirs` is set.
- Flatten-root with `--no-collect-empty-dirs` removes empty subdirectories instead of staging them.
- If CLI behavior changes, update both `README.md` and the launcher if needed.
