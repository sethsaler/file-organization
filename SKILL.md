---
name: organize-folder-by-filetype
description: Preview and organize files with category buckets, explainable routing rules, Needs Review, Downloader-to-Archive recipes, duplicate review, watch automation, and recoverable manifests.
version: 1.9.0
metadata:
  hermes:
    tags: [filesystem, organization, cleanup, file-management, optimization]
    related_skills: []
---

# Organize Folder by File Type (Optimized)

## When to use

Use this skill when a user wants a folder or folder tree previewed, organized, routed into **Images**, **Videos**, **GIFs**, and **Other**, held safely for review, or moved from a Downloader tree into an explicit Archive destination. Use it as well for watched-folder health, recovery, and local Dedupe handoff.

## Canonical source

Treat the repository working copy as the canonical source for this skill when one exists.

Keep changes synchronized across:

- `scripts/organize_by_filetype.py`
- `SKILL.md`
- `README.md`
- `launchers/Organize Files by Type.command` when launcher behavior is affected
- `scripts/command_center.py` when CLI flags or GUI options should stay aligned
- `scripts/schedule_config.py`, `scripts/schedule_gui.py`, and `scripts/schedule_daemon.py` when schedule JSON or runner behavior changes
- `scripts/org_rules.py`, `scripts/org_safety.py`, and `scripts/org_watch_status.py` when routing, quarantine, or live watch behavior changes

Use `README.md` for repository-facing documentation and `SKILL.md` for agent-facing operating instructions.

## Core behavior

- Buckets are **only** `Images`, `Videos`, `GIFs`, and `Other`. Files with `.gif` go to **GIFs** (not Images). Everything else unknown or without an extension goes to **Other**.
- No overwrite ever; collisions are suffixed (_1, _2, ...).
- Hidden files and folders are included by default (`--no-include-hidden` to exclude dotfiles).
- With default empty-folder handling (`--collect-empty-dirs`), collectable empty folder trees move to root-level `For Deletion` (multi-round passes), then leftover empty directories are removed (including empty bucket folders). Applies to flatten-root, in-place, and non-recursive runs. Use `--no-collect-empty-dirs` to skip staging.
- Optional `--detect-duplicates`: identical-content copies (size + BLAKE2 hash, hashed lazily only on size collisions) are staged into a root-level `Duplicates` folder instead of their bucket. Nothing is deleted; moves are in the backup manifest and `Duplicates` is skipped on later runs. Files already in root bucket folders count as the canonical copies. Cloud-placeholder files with no local data (undownloaded iCloud items) are never hashed, so runs cannot trigger downloads.
- Optional `--duplicates-hardlink` (with `--detect-duplicates`): duplicates stay in their bucket as hardlinks to the canonical copy (zero extra disk space) instead of moving to `Duplicates`; falls back to a plain move if the filesystem cannot hardlink.
- Optional `--date-buckets`: files land under `Bucket/YYYY/MM` (by modification time) in every mode.
- Optional `--rules FILE`: apply ordered, deterministic rules that can match extension, filename/path/parent globs, source URL, and file size. Every planned move includes a human-readable reason. Destinations are confined to the chosen root.
- Rule fallbacks are `bucket`, `needs-review`, or `leave`. Use `needs-review` when uncertain content should be held in root-level **Needs Review** instead of guessed or deleted.
- Optional Archive recipe (`--archive-root PATH --archive-mapping FILE`) routes known creator folders to explicit Archive-relative paths, loose media to exact `Recents/Images`, `Recents/Videos`, `Recents/GIFs`, or `Recents/Other` paths, and unknown creator folders to Archive **Needs Review**.
- External Archive runs write recovery manifests at both the source and Archive roots. Absolute restore paths are allowed only for explicit version-2 external manifests.
- **Needs Review**, **For Deletion**, and **Duplicates** are quarantine/review surfaces. Repeated organization runs skip them.

## Modes

- Non-recursive: only target folder direct files.
- `flatten-root`: every file under the tree (any depth) moves into buckets **directly under the chosen folder**. Traversal skips only **directories named** `For Deletion` or `.organizer` (at any depth). Legacy folders from an older sort (`JPG`, `MP4`, `PNG`, …) are always entered so files inside merge into the four category buckets.

## Normalization

- none: skip normalization.
- standard: canonical folder names for `Images`, `Videos`, `GIFs`, `Other` (case fixes on case-insensitive volumes).

Default recommendation:

- Recursive runs: use standard normalization.
- Non-recursive runs: normalization optional.

## Efficiency design

This skill uses one reusable helper script at `scripts/organize_by_filetype.py` to reduce tool chatter and repeated scans.

Performance characteristics:

- single command execution for main operation
- O(N) directory walk for movement stage
- top-down walk skips only `For Deletion` / `.organizer` (and `Duplicates` when duplicate detection is on); follows directory symlinks with inode cycle guard
- O(1) extension→bucket lookup via a precomputed map; per-directory `resolve()` caching on the move path
- optional bottom-up normalization pass only when requested
- structured JSON output for direct reporting
- dry-run mode for fast planning and validation without writes; the empty-folder preview is simulated in memory (no temp-dir tree clone)

## Project structure

- `scripts/organize_by_filetype.py` — main helper
- `scripts/command_center.py` — main Tk Command Center (Overview, Organize, Rules & Review, Safety Center, watched folders, history, and advanced tools)
- `scripts/tinker_gui.py` — legacy compact Tk UI
- `scripts/org_rules.py` — ordered routing rules, validation, learned approvals, and Archive recipe
- `scripts/org_safety.py` — quarantine inventory, Finder reveal, OS Trash, recovery, approval, and Dedupe handoff
- `scripts/org_watch_status.py` — atomic live watch-health snapshot used by the daemon and Overview
- `scripts/schedule_panel.py` — shared Schedule tab / panel (folder list, timing, worker)
- `scripts/schedule_config.py` — shared `schedule.json` schema and parallel organizer runs
- `scripts/schedule_gui.py` — schedule-only window (same panel as Tinker’s Schedule tab)
- `scripts/schedule_daemon.py` — background loop or `--once` for cron; runs enabled folders in parallel; in `watch` schedule mode the foreground loop reacts to native FS events via the optional `watchdog` package (near-instant, any depth; falls back to mtime polling without it) and organizes once a folder stays quiet
- `scripts/schedule_watch.py` — native filesystem-event monitor for watch mode (watchdog/FSEvents/inotify wrapper with polling fallback)
- `scripts/install.sh` — one-line curl installer (GitHub tarball into a chosen directory)
- `scripts/quick_controls.py` — status, pause/resume, run-all, undo-latest, and open-folder commands for native macOS controls
- `scripts/install_macos_integrations.py` — builds/installs the native menu-bar app and Finder Quick Action
- `macos/FileOrganizerMenuBar.swift` — native `FO` menu-bar application source
- **Background scheduling:** enabling automatic runs in the Schedule tab installs a LaunchAgent (macOS) or systemd user unit (Linux) so `schedule_daemon.py` keeps running after the app closes. See `scripts/schedule_service.py`.
- `launchers/Organize by File Type (Tinker).command` — macOS double-click for the Tk UI
- `launchers/File Organizer macOS Controls.command` — installs and launches the optional menu-bar helper and Finder Quick Action
- `launchers/Organize Desktop by File Type.command` — one-click `~/Desktop` run (recursive flatten-root, standard normalization, `For Deletion` staging by default)
- `launchers/Organize Files by Type.command` — prompts for a folder, then flatten-root + standard normalization + `For Deletion` staging (dry-run preview, then confirm)
- `README.md` — repository-facing documentation
- `SKILL.md` — agent-facing skill instructions

## Execution workflow

1. Confirm user inputs

- target path
- For CLI/Tinker: recursive vs non-recursive, strategy, normalization, hidden, empty-folder handling, dry-run as needed
- optional rules file and unmatched fallback, or an explicit Archive root plus creator mapping (rules and Archive mode are mutually exclusive)
- For `Organize Files by Type.command`: only the folder path (behavior is fixed: recursive flatten-root, standard normalization, `For Deletion` staging, dry-run then confirm)

2. Run helper script

- Script location: `scripts/organize_by_filetype.py`
- Use terminal to run the Python script with the requested flags.
- Prefer one call per job (optionally a dry-run first, then real run).

3. Parse JSON output and report

Always report:

- target
- mode and strategy
- normalization mode
- files moved total
- counts by bucket (`moved_by_category`)
- collisions resolved
- folders touched
- normalization stats
- empty-folder collection / removal stats
- routing stats, including matched rules, unmatched count, Needs Review count, and external moves
- verification summary (root remaining files, noncanonical bucket dirs)

## Required safety rules

- Never delete user files.
- Never overwrite files.
- Preview before live organization, especially for rules or Archive routing.
- Treat `Needs Review`, `For Deletion`, and `Duplicates` as review queues. Reveal or restore first; moving a held file to the macOS Trash must be an explicit user action and must never unlink it directly.
- Never infer an Archive creator destination. Require an explicit mapping; unmatched creator folders go to Archive `Needs Review`.
- For external Archive moves, keep both recovery manifests and validate the destination root before running live.
- Flatten-root is the default mode; files consolidate into root-level buckets. Collectable empty trees go to `For Deletion` by default, then leftover empties are removed.
- Preserve hierarchy in recursive in-place mode.
- Empty-folder collection is on by default and must move folders into `For Deletion`, not delete them outright, before the final empty-dir trim.
- With `--no-collect-empty-dirs`, staging is skipped and empty directories are only removed in place.
- Treat case-only folder normalization safely on case-insensitive filesystems (temporary rename sequence).

## Notes

- For very large trees, do a dry run first to estimate scope.
- Use `--no-include-hidden` when the user wants dotfiles and dot-directories left alone.
- If the user asks to normalize folder casing after organization, run with `--normalize standard`.
- For non-recursive and in-place modes, empty-folder staging into `For Deletion` is the default unless `--no-collect-empty-dirs` is set; flatten-root uses the same default.
- With `--no-collect-empty-dirs`, empty subdirectories are removed in place only (no staging).
- iCloud Drive paths (`Mobile Documents/com~apple~CloudDocs/...`) work like normal folders; ensure files are downloaded locally if moves fail on placeholders.
- If CLI behavior changes, update both `README.md` and the launcher if needed.
