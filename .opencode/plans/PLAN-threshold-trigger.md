# Plan: Threshold-Triggered File Organization for Recents
## Goal
Trigger the file organizer to run on the **Recents** folder whenever more than 20 files are sitting unsorted at the folder root. Replace the current daily 2 AM schedule with a 30-minute interval poll that checks a configurable threshold before running.
## Current State
- **LaunchAgent** (`org.fileorganization.schedule-daemon.plist`) fires `schedule_daemon.py --once` daily at 2:00 AM
  
- `schedule.json` has two folders: Recents (`flatten-root`) and Manual Library (`in-place`)
  
- Recents currently has 0 loose files at root (all sorted into `Images/`, `Videos/`, `GIFs/`, `Other/`)
  
- After a `flatten-root` run, "unsorted" = files directly at the folder root (not inside bucket folders)
  
- The scheduler is purely time-based — no threshold or event-driven logic exists
  
## Design
### How "unsorted" is defined

The definition depends on the job's strategy:

**`flatten-root` (Recents):** A run moves all loose root files into bucket subdirectories (`Images/`, `Videos/`, `GIFs/`, `Other/`). After a successful run, the only things at root are bucket folders plus `For Deletion/` and `.organizer/`. So:

> **Unsorted count = number of regular files directly at the folder root** (not inside any subdirectory, excluding `.DS_Store`)

This is fast to compute — a single `Path.iterdir()` with no recursion.

**`in-place` (Manual Library):** A run walks the tree and within each directory moves loose files into bucket subdirs *at that same level*. So unsorted files can exist at any directory depth — any file that sits directly inside a non-bucket directory. The count must be recursive:

> **Unsorted count = sum of regular files in every directory that is not a bucket folder name** (excluding `.DS_Store`, skipping `For Deletion/`, `.organizer/`, and bucket-named dirs like `Images/`, `Videos/`, etc.)

Implementation: walk the tree with `os.walk()`, skip directories whose names match bucket names (from the job's profile) or `For Deletion` / `.organizer`, count files in each remaining directory (excluding `.DS_Store`). This is more expensive than the root-only check but Manual Library is not expected to be enormous, and it only runs every 30 min.
### Threshold gating
Each `FolderJob` gets an optional `min_unsorted_threshold` (default 0 = disabled, always run). When the scheduler processes a job:

1. If `min_unsorted_threshold > 0`, count unsorted files at the job's root
  
2. If count < threshold, **skip** that job and log the skip
  
3. If count >= threshold, run normally
  
### Schedule mode switch
- Change `schedule_mode` from `"daily"` to `"interval"` with `interval_minutes: 30`
  
- For launchd interval mode with `--once`: use `StartInterval` (every 30 min × 60 sec = 1800s) instead of `StartCalendarInterval`
  
- No `KeepAlive` or `RunAtLoad` — launchd fires `--once` every 30 min, the script exits after each batch
  
- Threshold gating means most polls will be no-ops (skip Recents, still run Manual Library)
  
## Changes
### 1. `scripts/schedule_config.py`
**Add** `count_unsorted_files(job: FolderJob) -> int`**:**

- Normalize the path via `normalize_folder_input()`
  
- **If `job.strategy == "flatten-root"` or not recursive:** count regular files (not directories, not `.DS_Store`) directly at root via `Path.iterdir()`
  
- **If `job.strategy == "in-place"` and recursive:** walk with `os.walk()`, skip directories whose names match bucket names (from `resolve_profile(job.profile)`) or `For Deletion` / `.organizer`, count regular files (excluding `.DS_Store`) in each remaining directory. **Early-abort optimization:** accept an optional `stop_at: int` parameter — once the count reaches this value, stop walking and return immediately. The scheduler passes `stop_at=job.min_unsorted_threshold` so we don't scan the entire tree when we already know the threshold is met.
  
- Return 0 if path doesn't exist or isn't a directory
  

**Add field to** `FolderJob`**:**

```python
min_unsorted_threshold: int = 0
```

**Update** `FolderJob` **serialization:**

- `to_json_dict()` already uses `asdict()` so the new field flows through automatically
  
- `from_json_dict()` — add `min_unsorted_threshold=int(item.get("min_unsorted_threshold", 0))`
  

**Update** `run_enabled_folders()`**:**

- Before building the task list for a job, if `job.min_unsorted_threshold > 0`:
  
  - Call `count_unsorted_files(job.path)`
    
  - If count < threshold, log skip and continue (don't add to `tasks`)
    
  - Record skip reason in the log
    
### 2. `scripts/schedule_service.py`
**Update** `build_launchd_plist()` **for interval mode:**

- Currently interval mode uses `--foreground` + `KeepAlive` (long-running daemon)
  
- Add support for `--once` + `StartInterval` (fire-and-exit every N seconds)
  
- Logic: if `schedule_mode == "interval"` and `interval_minutes <= some cutoff` (e.g., ≤ 60 min), use `StartInterval` + `--once` (more efficient than a 24/7 daemon for short intervals)
  
- Otherwise keep the existing `--foreground` + `KeepAlive` approach for long intervals
  

`StartInterval` **value:** `interval_minutes * 60` seconds (e.g., 30 min → 1800 sec)

**Wrap with** `caffeinate` (same as daily mode) to prevent sleep during the run.
### 3. `~/.config/file-organization/schedule.json`
Update the Recents folder entry:

```json
{
  "path": "/Users/sethsaler/Library/Mobile Documents/com~apple~CloudDocs/System Files/Archive/Recents",
  "enabled": true,
  "min_unsorted_threshold": 20,
  ...
}
```

Update top-level config:

```json
{
  "schedule_mode": "interval",
  "interval_minutes": 30
}
```

Manual Library entry: also set `min_unsorted_threshold: 20` (same threshold, but the count is recursive since it uses `in-place` strategy).
### 4. Rebuild and reload LaunchAgent
Run `schedule_service.py`'s `restart_service()` (or the equivalent sequence):

1. `stop_service()` — unload the existing agent
  
2. `start_service()` — rebuild plist with `StartInterval` + `--once`, load it
  

This will be done via the existing GUI or CLI, not by hand-editing the plist.
### 5. Tests — `tests/test_organize.py`
- `test_count_unsorted_files_flatten_root`: create a temp dir with N loose files + bucket subdirs; verify count = N (excludes subdirs, `.DS_Store`, files inside subdirs)
  
- `test_count_unsorted_files_in_place`: create a nested tree with loose files in non-bucket subdirs + files inside bucket subdirs; verify count = loose files only (skips bucket-named dirs, `For Deletion`, `.organizer`)
  
- `test_threshold_skips_below`: folder with < threshold files; verify `run_enabled_folders` skips it
  
- `test_threshold_runs_at_or_above`: folder with >= threshold files; verify it runs
  
- `test_threshold_zero_always_runs`: default `min_unsorted_threshold=0` doesn't gate
  
- `test_launchd_interval_uses_start_interval`: verify `build_launchd_plist()` returns `StartInterval` for interval mode with short interval
  
### 6. GUI — `scripts/schedule_panel.py` (optional but recommended)
Add a "Min unsorted files" spinbox to the per-folder options in the Schedule tab, so the threshold is configurable without editing JSON. Default value: 0.
### 7. Documentation
- Update `README.md` scheduler section to mention threshold-gated runs
  
- Update `CHANGELOG.md` under "Unreleased"
  
## Flow After Implementation
```
launchd fires --once every 30 min
  → schedule_daemon.py --once
    → load schedule.json
    → for Recents folder (flatten-root):
        count = count_unsorted_files(job)  # root-level only
        if count < 20: skip, log "skipped (3 < 20 unsorted)"
        else: run organize_by_filetype.py --path ... --flatten-root
    → for Manual Library folder (in-place):
        count = count_unsorted_files(job)  # recursive, skips bucket dirs
        if count < threshold: skip
        else: run organize_by_filetype.py --path ... --in-place
    → save config (update last_run)
    → exit
```

> **Note:** Both folders now have threshold gating. Manual Library also gets a `min_unsorted_threshold` (suggest 20 as well, configurable). The `count_unsorted_files` function is strategy-aware: root-only for `flatten-root`, recursive for `in-place` (skipping bucket-named directories).
## Edge Cases
- **iCloud sync delays**: Files may appear gradually. The 30-min poll means we catch them within 30 min of crossing the threshold. Not a problem.
  
- **Path doesn't exist**: `count_unsorted_files` returns 0, job skips (existing behavior already skips missing paths).
  
- **Race condition with iCloud**: If files are still syncing, the organizer might catch a partial batch. This is fine — the next poll picks up the rest. The organizer is idempotent.
  
- `.DS_Store` **files**: Excluded from the unsorted count (they're not real content).
  
- **Already-sorted state**: After a run, root has only bucket dirs → count = 0 → skip. Efficient.
  
- **Manual Library threshold**: Manual Library also gets `min_unsorted_threshold: 20`. The count is recursive for `in-place` strategy — it walks all subdirectories, skipping bucket-named dirs (`Images/`, `Videos/`, etc.), `For Deletion/`, and `.organizer/`, counting loose files in every other directory. The early-abort optimization means the walk stops as soon as count >= threshold, so for a library with thousands of sorted files + 20 unsorted, we stop after finding the 20th unsorted file rather than scanning everything.
  
## Files Modified
| File | Change |
|------|--------|
| `scripts/schedule_config.py` | Add `count_unsorted_files()`, `min_unsorted_threshold` field, threshold gating in `run_enabled_folders()` |
| `scripts/schedule_service.py` | Support `StartInterval` + `--once` for interval mode in `build_launchd_plist()` |
| `tests/test_organize.py` | Add threshold gating tests |
| `scripts/schedule_panel.py` | (Optional) Add threshold spinbox to GUI |
| `README.md` | Document threshold feature |
| `CHANGELOG.md` | Entry under Unreleased |
## Config changes (not code, applied at runtime)
| Target | Change |
|--------|--------|
| `~/.config/file-organization/schedule.json` | `schedule_mode: "interval"`, `interval_minutes: 30`, Recents `min_unsorted_threshold: 20` |
| `~/Library/LaunchAgents/org.fileorganization.schedule-daemon.plist` | Rebuilt by `schedule_service.py` to use `StartInterval` |

---
comments:
  c1:
    by: user
    at: "2026-06-30T19:53:31.650Z"
  c1-reply:
    body: >
      Good catch. Updated: `count_unsorted_files` is now strategy-aware.
      For `in-place` (Manual Library), it walks recursively with `os.walk()`,
      skipping bucket-named dirs, `For Deletion/`, and `.organizer/`.
      Early-abort stops the walk once count >= threshold. Both folders get
      `min_unsorted_threshold: 20`.
    by: AI
    at: "2026-06-30T20:05:00.000Z"
    re: c1
