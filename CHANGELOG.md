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

## [1.9.0] - 2026-07-14

### Added

- **Near-instant watch mode via native filesystem events:** with the optional [`watchdog`](https://pypi.org/project/watchdog/) package installed (`pip install "organize-folder-by-filetype[watch]"`; the curl installer adds it automatically), the watch daemon subscribes to native FS events (FSEvents on macOS, inotify on Linux) with one recursive watch per enabled folder — changes at **any depth** are detected in milliseconds instead of waiting up to 5 s for the recursive mtime scan. A relaxed 60 s full-scan safety net catches any dropped events, and the organizer's own moves plus noise files (`.DS_Store`, `.organizer/` manifests) are filtered so runs don't retrigger themselves. Without watchdog, the previous tiered mtime polling is used unchanged. The daemon logs the active backend at startup (`"backend": "fsevents" | "inotify" | "polling"`). New module: `scripts/schedule_watch.py`.

### Changed

- **macOS watch mode now runs a persistent daemon instead of launchd `WatchPaths`:** the one-shot `WatchPaths` agent imposed a hard 15 s `ThrottleInterval` and only saw top-level changes; enabling automatic runs in watch mode now installs a `--foreground` agent with `RunAtLoad` + `KeepAlive` running the event-driven watch loop. Watched folders are picked up from `schedule.json` on the daemon's periodic config reload, so adding/removing folders no longer requires reinstalling the agent.
- **Snappier watch debounce:** `watch_quiet_seconds` default lowered from 1.0 s to 0.3 s. In-progress copies keep emitting events/mtime bumps that reset the quiet timer, so large files still finish before being moved. End-to-end, a file dropped anywhere in a watched tree is now typically organized in ~0.3–0.5 s (previously ~6–20 s depending on depth and platform).

- **Duplicate hardlinking (`--duplicates-hardlink`):** with `--detect-duplicates`, duplicates stay in their bucket as hardlinks to the canonical copy instead of being staged into `Duplicates` — the copy costs no disk space and both paths remain valid. Falls back to a plain move if the filesystem cannot hardlink. Toggle in the Tinker GUI and Schedule tab (`duplicates_hardlink` in `schedule.json`).
- **Date-based bucketing (`--date-buckets`):** files are placed under `Bucket/YYYY/MM` subfolders by modification time, in every strategy. Toggle in the Tinker GUI and Schedule tab (`date_buckets` in `schedule.json`).
- **iCloud-safe duplicate detection:** cloud-placeholder files with no local content (undownloaded iCloud Drive items, detected via `SF_DATALESS` / zero allocated blocks) are never registered for duplicate hashing, so background sweeps can no longer trigger surprise multi-gigabyte downloads.
- **Watch-mode LaunchAgent runs only the changed folder:** `schedule_daemon.py --once` in watch mode persists per-folder watch signatures (`~/.local/state/file-organization/watch-signatures.json`) and organizes only folders whose signature changed since the last fire. A fire with no signature changes (the hourly `StartInterval` backstop) still sweeps everything to catch deep changes the signature cannot see.
- **Watch mode is now concurrent and non-blocking:** the `--foreground` daemon uses a `ThreadPoolExecutor` so each watched folder is organized in its own background worker. The main poll loop continues to watch for changes while other folders are running, and a long-running folder no longer blocks the others. Config saves use a lock + merge to keep state safe across concurrent runs. Polling/quiet defaults are now 0.25 s / 1 s for faster reaction, and both are configurable per-config (`watch_poll_seconds`, `watch_quiet_seconds`) or in the Schedule tab. Polling is now tiered: a lightweight fast signature (watched root + immediate subdirs) runs every poll, and a full recursive scan runs every 5 s to catch deep changes without heavy frequent work.
- **Watch mode now detects changes at any depth:** the mtime signature now recursively covers all directories under the watched root, so files added or changed in nested subfolders (e.g. `Manual Library/Ella/...`) trigger the watcher.
- **macOS notifications after background sweeps:** the daemon posts a notification ("Organized N folder(s), M failed") after scheduled/watch runs that actually ran folders. Disable with `"notify_on_run": false` in `schedule.json`.
- **Run history:** every scheduled/manual batch appends a compact record (path, outcome, files moved, duplicates, empty dirs staged) to `~/.local/state/file-organization/history.jsonl` (auto-trimmed at ~1 MiB). New **History…** button in the Schedule tab shows the recent runs.
- **Per-folder run timeout:** `timeout_minutes` on a folder job (default 60, 0 = no timeout) replaces the fixed 1-hour cap on scheduled runs.
- **Background scheduler self-heals across restarts, OS updates, and Homebrew Python upgrades:** service files now bake in a version-stable interpreter symlink (`/opt/homebrew/bin/python3` / `/usr/local/bin/python3`) instead of a versioned path like `python@3.14` that vanishes on `brew upgrade`. `start_service()` detects a stale installed LaunchAgent (missing interpreter, or any drift from the current config) and force-reloads it, and clears a persisted `launchctl disable` / Background Task Management toggle before bootstrapping. Since the GUIs already re-sync the service on launch when automatic runs are enabled, opening either GUI now repairs a silently broken agent.
- **Duplicate detection (`--detect-duplicates`):** files with identical content are detected during organization (size-first grouping, lazy BLAKE2 hashing — unique-sized files are never read) and the copies are staged into a root-level `Duplicates` folder instead of their bucket. The first copy encountered is canonical; files already inside root bucket folders are seeded as canonical so re-runs keep the organized copy. `Duplicates` is skipped on later runs, moves are recorded in the backup manifest (restorable), and the JSON summary gains a `duplicates` block. Available as a per-folder toggle in the Schedule tab and a checkbox in the Tinker GUI (`detect_duplicates` in `schedule.json`).
- **Watch schedule mode (near real-time organizing):** `schedule_mode: "watch"` organizes a folder shortly after files change instead of polling on a timer. On macOS the LaunchAgent uses native `WatchPaths` (event-driven, no resident daemon, `ThrottleInterval` 15 s debounce, hourly `StartInterval` backstop). The `--foreground` daemon (systemd/manual) gains a watch loop that polls a cheap mtime signature (folder root + immediate subdirs) every 2 s and fires after a 5 s quiet period, running only the folder(s) that changed. New GUI radio option: "Watch folders and organize when files change".
- **Threshold-gated scheduling:** each `FolderJob` supports `min_unsorted_threshold` (default 0 = always run). When > 0, the scheduler counts unsorted files before running — if below the threshold, the folder is skipped. The count is strategy-aware: root-only for `flatten-root`, recursive for `in-place` (skipping bucket-named dirs, `For Deletion`, `.organizer`). Early-abort stops the walk once the threshold is met. Configurable via the Schedule tab GUI.
- **LaunchAgent interval mode with `StartInterval`:** short intervals (≤ 60 min) now use `StartInterval` + `--once` (fire-and-exit every N seconds) instead of a 24/7 `--foreground` daemon. More efficient for frequent polling.
- **Background scheduler from GUI:** enabling automatic runs installs a LaunchAgent (macOS) or systemd user unit (Linux) via `schedule_service.py`, so scheduled organization continues after the app closes.
- **Scheduler in main GUI:** `tinker_gui.py` has a **Schedule** tab (`schedule_panel.py`) with folder list, daily/interval timing, next-run status, and background daemon control. **Add to schedule…** on the Organize tab links the current folder. `schedule_gui.py` remains a schedule-only window using the same panel.
- `scripts/install.sh`: curl-friendly installer that unpacks a GitHub ref into `~/.local/share/organize-folder-by-filetype` (configurable via `FILE_ORG_*` env vars).
- `scripts/tinker_gui.py`: Tkinter UI to configure options and run dry-run or live organize with JSON output.
- `launchers/Organize by File Type (Tinker).command`: macOS launcher for the tinker GUI.

### Performance

- **Exclusion checks no longer resolve paths repeatedly:** `path_excluded` derives relative paths with a string prefix strip instead of two `resolve()` chains, and per-directory skip decisions are memoized across the run's 5-7 tree passes (`_at_organize_root` also now uses the existing resolve cache). Previously each directory paid multiple lstat/readlink chains per pass.
- **Fewer full-tree walks per run:** the normalize pass reuses the walk's directory listings instead of re-listing every directory; empty-`Other` cleanup uses dirs observed during traversal instead of an `rglob("Other")` sweep; the two verification walks are folded into one; empty-dir inspection uses `os.scandir` d_type info instead of 2-3 stats per entry.
- **Per-file fast paths:** same-volume moves use a single `os.rename` syscall (falling back to `shutil.move` cross-device); collision-name probing resumes from the last suffix per (dir, name) instead of restarting at `_1` (was O(n²) for n same-named files); extension parsing uses `splitext` instead of Path construction; the random-rename pass and duplicate-index seeding use one `lstat` per file instead of 2-3 stats, and the rename pass no longer stats for destination existence; `DuplicateIndex.update_location` is O(1) via a reverse path→digest map; symlink-cycle bookkeeping is skipped entirely when not following symlinks.
- **Dry-run empty-folder preview is now simulated in memory** instead of cloning the whole tree into a temp directory and running a second organizer over it (~8× faster dry runs on a 5,000-file tree; the gap grows with tree size).
- **Per-file move overhead cut:** destination checks use a cached per-directory `resolve()` (previously 3 uncached `resolve()` chains per file) and `mkdir` runs once per destination directory. Real runs ~1.5× faster on 5k–20k-file trees.
- **`.DS_Store` cleanup no longer opens every extensionless file:** only 16-char random-rename candidates are content-sniffed, and the scan uses one `os.walk` instead of `rglob` with per-path `Path` objects.
- **O(1) extension classification:** bucket lookup uses a precomputed extension→bucket dict instead of scanning every bucket's extension set per file.

### Fixed

- **Folders containing only a `.DS_Store` are now removed/staged:** `.DS_Store` cleanup runs before the empty-dir passes, so such folders count as empty even with hidden files included (previously they were left behind unless `include_hidden` was off).
- `org_buckets.py` referenced `Optional` without importing it (latent `NameError`).
- `expand_subfolders` now propagates `min_unsorted_threshold` to the per-subfolder jobs (it was silently dropped).
- Changing the interval minutes (not just mode/daily time) now reinstalls the background agent, since `StartInterval` is baked into the plist.

### Changed

- **Removed per-extension mode:** `--by-extension` is gone. Output JSON uses `moved_by_category`, `buckets`, and `noncanonical_bucket_dirs_*`; `alias_map` dropped from normalization stats.
- **Traversal / legacy buckets:** Directory walking no longer skips folders whose names look like extension or category buckets (e.g. root-level `JPG/` or `Images/`). Only `For Deletion` and `.organizer` are skipped, so trees like iCloud `PreSorted/JPG/` are scanned. **Idempotent moves:** files already inside the correct destination folder are not moved again.
- **Flatten-root + empty folders:** With default `--collect-empty-dirs`, flatten-root no longer deletes empty trees before collection; collectable trees are moved to `For Deletion` in multiple rounds, then a final pass removes leftover empty dirs (including empty bucket folders). Repo launchers and Desktop helper now use staging for flatten as well. `--no-collect-empty-dirs` still skips staging.
- **Category-only buckets:** Default organization is **Images**, **Videos**, **GIFs**, and **Other** (GIFs separate from still images). Tinker no longer offers an extension-folder toggle.

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
