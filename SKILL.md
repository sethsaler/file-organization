---
name: organize-folder-by-filetype
description: Efficient file-type organization with a single optimized Python helper (non-recursive/recursive, optional normalization, dry-run, and collision-safe moves).
version: 1.3.1
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

Use `README.md` for repository-facing documentation and `SKILL.md` for agent-facing operating instructions.

## Core behavior

- Buckets are uppercase extension folders (for example jpg -> JPG).
- Files without extension go to NO_EXTENSION.
- No overwrite ever; collisions are suffixed (_1, _2, ...).
- Hidden files and folders are excluded by default.

## Modes

- Non-recursive (default): only target folder direct files.
- Recursive:
  - in-place (default): each directory organizes its own direct files.
  - flatten-root (explicit only): full tree consolidates into root buckets.

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
- `launchers/Organize Files by Type.command` — optional macOS quick launcher
- `README.md` — repository-facing documentation
- `SKILL.md` — agent-facing skill instructions

## Execution workflow

1) Confirm user inputs
- target path
- recursive or non-recursive
- strategy (if recursive)
- include hidden (default no)
- normalization mode
- dry-run yes/no

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
- verification summary (root remaining files, noncanonical dirs)

## Required safety rules

- Never delete user files.
- Never overwrite files.
- Do not flatten unless explicitly requested.
- Preserve hierarchy in recursive in-place mode.
- Treat case-only folder normalization safely on case-insensitive filesystems (temporary rename sequence).

## Notes

- For very large trees, do a dry run first to estimate scope.
- If hidden files should be included, the user must explicitly request it.
- If the user asks to normalize aliases or casing after organization, run with `--normalize standard`.
- If CLI behavior changes, update both `README.md` and the launcher if needed.
