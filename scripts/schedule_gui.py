#!/usr/bin/env python3
"""Tk GUI: designate folders to organize on a schedule.

Uses the same JSON config as `schedule_daemon.py` (~/.config/file-organization/schedule.json).
Enable **automatic runs** for an in-window timer, or run **`schedule_daemon.py --foreground`**
with systemd/cron for true background scheduling. Enabled folders run **in parallel** (see max parallel).
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from schedule_config import (
    FolderJob,
    SCHEDULE_MODE_DAILY,
    SCHEDULE_MODE_INTERVAL,
    build_organize_cmd,
    effective_normalize,
    default_config_path,
    load_config,
    normalize_daily_time,
    normalize_schedule_mode,
    organizer_script_path,
    run_dry_run_preview,
    run_enabled_folders,
    save_config,
    wait_seconds_after_run,
)


class ScheduleApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config_path = default_config_path()
        self.cfg = load_config(self.config_path)

        self.root.title("Organize on a schedule")
        self.root.minsize(640, 560)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._cfg_lock = threading.Lock()

        self.interval_var = tk.IntVar(value=self.cfg.interval_minutes)
        self.schedule_mode_var = tk.StringVar(
            value=normalize_schedule_mode(self.cfg.schedule_mode)
        )
        self.daily_time_var = tk.StringVar(value=normalize_daily_time(self.cfg.daily_time))
        self.scheduler_var = tk.BooleanVar(value=self.cfg.scheduler_enabled)
        self.max_parallel_var = tk.IntVar(value=self.cfg.max_parallel)

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)

        row = 0
        top = ttk.LabelFrame(frm, text="Schedule", padding=8)
        top.grid(row=row, column=0, sticky="ew", **pad)
        top.columnconfigure(2, weight=1)

        mode_row = ttk.Frame(top)
        mode_row.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="Run every",
            variable=self.schedule_mode_var,
            value=SCHEDULE_MODE_INTERVAL,
            command=self._on_schedule_mode_change,
        ).pack(side="left")
        self.interval_spin = tk.Spinbox(
            mode_row,
            from_=1,
            to=10080,
            width=8,
            textvariable=self.interval_var,
            command=self._sync_interval_to_cfg,
        )
        self.interval_spin.pack(side="left", padx=(6, 0))
        ttk.Label(mode_row, text="minutes").pack(side="left", padx=(6, 0))

        daily_row = ttk.Frame(top)
        daily_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Radiobutton(
            daily_row,
            text="Once daily at",
            variable=self.schedule_mode_var,
            value=SCHEDULE_MODE_DAILY,
            command=self._on_schedule_mode_change,
        ).pack(side="left")
        self.daily_time_entry = ttk.Entry(daily_row, width=7, textvariable=self.daily_time_var)
        self.daily_time_entry.pack(side="left", padx=(6, 0))
        self.daily_time_entry.bind("<FocusOut>", lambda _e: self._sync_daily_time_to_cfg())
        self.daily_time_entry.bind("<Return>", lambda _e: self._sync_daily_time_to_cfg())
        ttk.Label(daily_row, text="(local time, 24h — default midnight)").pack(side="left", padx=(6, 0))

        ttk.Label(top, text="Max parallel").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        tk.Spinbox(
            top,
            from_=0,
            to=128,
            width=8,
            textvariable=self.max_parallel_var,
            command=self._sync_parallel_to_cfg,
        ).grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(top, text="(0 = all enabled folders at once, max 32)").grid(row=2, column=2, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Checkbutton(
            top,
            text="Enable automatic runs (in this window)",
            variable=self.scheduler_var,
            command=self._on_scheduler_toggle,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        hint = (
            "Background: run scripts/schedule_daemon.py --foreground under systemd "
            "(daily mode sleeps until the next local run time), or schedule_daemon.py --once "
            "via cron at midnight (0 0 * * *). Same config file as shown below."
        )
        ttk.Label(top, text=hint, wraplength=620, justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self._on_schedule_mode_change()

        row += 1
        path_row = ttk.Frame(frm)
        path_row.grid(row=row, column=0, sticky="ew", **pad)
        path_row.columnconfigure(0, weight=1)
        ttk.Label(path_row, text=f"Config: {self.config_path}").grid(row=0, column=0, sticky="w")
        ttk.Button(path_row, text="Save now", command=self._save).grid(row=0, column=1, padx=(8, 0))
        row += 1

        mid = ttk.LabelFrame(frm, text="Folders to organize", padding=8)
        mid.grid(row=row, column=0, sticky="nsew", **pad)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        list_frame = ttk.Frame(mid)
        list_frame.grid(row=0, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            list_frame,
            columns=("enabled", "path", "last_run"),
            show="headings",
            height=8,
            selectmode="browse",
        )
        self.tree.heading("enabled", text="On")
        self.tree.heading("path", text="Folder")
        self.tree.heading("last_run", text="Last run")
        self.tree.column("enabled", width=44, stretch=False, anchor="center")
        self.tree.column("path", width=360, stretch=True)
        self.tree.column("last_run", width=160, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        btn_row = ttk.Frame(mid)
        btn_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btn_row, text="Add folder…", command=self._add_folder).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Remove", command=self._remove_selected).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Run selected now", command=self._run_selected_now).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Run all enabled now", command=self._run_all_enabled_now).pack(side="left")

        row += 1
        detail = ttk.LabelFrame(frm, text="Selected folder options", padding=8)
        detail.grid(row=row, column=0, sticky="ew", **pad)
        detail.columnconfigure(1, weight=1)

        self.enabled_var = tk.BooleanVar(value=True)
        self.recursive_var = tk.BooleanVar(value=True)
        self.strategy_var = tk.StringVar(value="flatten-root")
        self.normalize_var = tk.StringVar(value="standard")
        self.hidden_var = tk.BooleanVar(value=True)
        self.collect_empty_var = tk.BooleanVar(value=True)

        dr = 0
        ttk.Checkbutton(detail, text="Include in scheduled runs", variable=self.enabled_var, command=self._push_detail_to_job).grid(
            row=dr, column=0, columnspan=2, sticky="w", pady=2
        )
        dr += 1
        ttk.Checkbutton(detail, text="Recursive", variable=self.recursive_var, command=self._push_detail_to_job).grid(
            row=dr, column=0, columnspan=2, sticky="w", pady=2
        )
        dr += 1
        strat = ttk.LabelFrame(detail, text="Recursive strategy", padding=6)
        strat.grid(row=dr, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Radiobutton(strat, text="Flatten to root buckets", variable=self.strategy_var, value="flatten-root", command=self._push_detail_to_job).pack(
            anchor="w"
        )
        ttk.Radiobutton(strat, text="In-place", variable=self.strategy_var, value="in-place", command=self._push_detail_to_job).pack(anchor="w")
        dr += 1
        norm = ttk.LabelFrame(detail, text="Normalization", padding=6)
        norm.grid(row=dr, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Radiobutton(norm, text="Standard", variable=self.normalize_var, value="standard", command=self._push_detail_to_job).pack(anchor="w")
        ttk.Radiobutton(norm, text="None", variable=self.normalize_var, value="none", command=self._push_detail_to_job).pack(anchor="w")
        dr += 1
        ttk.Checkbutton(detail, text="Include hidden files and folders", variable=self.hidden_var, command=self._push_detail_to_job).grid(
            row=dr, column=0, columnspan=2, sticky="w", pady=2
        )
        dr += 1
        ttk.Checkbutton(
            detail,
            text="Collect empty folders into “For Deletion”",
            variable=self.collect_empty_var,
            command=self._push_detail_to_job,
        ).grid(row=dr, column=0, columnspan=2, sticky="w", pady=2)

        row += 1
        ttk.Label(frm, text="Log:").grid(row=row, column=0, sticky="w", **pad)
        row += 1
        mono = ("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10)
        self.out = scrolledtext.ScrolledText(frm, height=10, wrap="word", font=mono)
        self.out.grid(row=row, column=0, sticky="nsew", **pad)
        frm.rowconfigure(row, weight=1)

        self._tree_item_to_index: Dict[str, int] = {}
        self._refresh_tree()
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        helper = organizer_script_path()
        if not helper.is_file():
            self._append_log(f"Missing helper script:\n{helper}\n")

        if self.cfg.scheduler_enabled:
            self._ensure_worker()

    def _on_schedule_mode_change(self) -> None:
        daily = normalize_schedule_mode(self.schedule_mode_var.get()) == SCHEDULE_MODE_DAILY
        spin_state = "disabled" if daily else "normal"
        try:
            self.interval_spin.configure(state=spin_state)
        except tk.TclError:
            pass
        entry_state = "normal" if daily else "disabled"
        try:
            self.daily_time_entry.configure(state=entry_state)
        except tk.TclError:
            pass
        self._sync_schedule_timing_to_cfg()

    def _sync_schedule_timing_to_cfg(self) -> None:
        self._sync_interval_to_cfg(quiet=True)
        self._sync_daily_time_to_cfg(quiet=True)
        mode = normalize_schedule_mode(self.schedule_mode_var.get())
        prev_mode = normalize_schedule_mode(self.cfg.schedule_mode)
        prev_time = normalize_daily_time(self.cfg.daily_time)
        time_val = normalize_daily_time(self.daily_time_var.get())
        self.cfg.schedule_mode = mode
        self.cfg.daily_time = time_val
        self.daily_time_var.set(time_val)
        if mode != prev_mode or time_val != prev_time:
            self._wake_event.set()

    def _sync_interval_to_cfg(self, *, quiet: bool = False) -> None:
        try:
            v = int(self.interval_var.get())
        except (tk.TclError, ValueError):
            v = 60
        v = max(1, min(10080, v))
        self.interval_var.set(v)
        prev = self.cfg.interval_minutes
        self.cfg.interval_minutes = v
        if not quiet and v != prev:
            self._wake_event.set()

    def _sync_daily_time_to_cfg(self, *, quiet: bool = False) -> None:
        normalized = normalize_daily_time(self.daily_time_var.get())
        self.daily_time_var.set(normalized)
        prev = normalize_daily_time(self.cfg.daily_time)
        self.cfg.daily_time = normalized
        self.cfg.schedule_mode = normalize_schedule_mode(self.schedule_mode_var.get())
        if not quiet and normalized != prev:
            self._wake_event.set()

    def _sync_parallel_to_cfg(self) -> None:
        try:
            v = int(self.max_parallel_var.get())
        except (tk.TclError, ValueError):
            v = 0
        v = max(0, min(128, v))
        self.max_parallel_var.set(v)
        self.cfg.max_parallel = v

    def _on_scheduler_toggle(self) -> None:
        self.cfg.scheduler_enabled = bool(self.scheduler_var.get())
        self._wake_event.set()
        if self.cfg.scheduler_enabled:
            self._ensure_worker()
            self._append_log("Automatic runs enabled in this window (parallel batches).\n")
        else:
            self._append_log("Automatic runs in this window disabled.\n")
        self._save_quiet()

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="schedule-worker", daemon=True)
        self._worker.start()

    def _interruptible_wait(self, seconds: float) -> bool:
        """Wait up to `seconds`. Return True if shutdown requested."""
        remaining = float(seconds)
        while remaining > 0:
            if self._stop_event.is_set():
                return True
            if self._wake_event.is_set():
                self._wake_event.clear()
                return False
            chunk = min(remaining, 1.0)
            if self._stop_event.wait(timeout=chunk):
                return True
            if self._wake_event.is_set():
                self._wake_event.clear()
                return False
            remaining -= chunk
        return False

    def _wait_interval(self, seconds: float) -> str:
        """Wait up to `seconds`. Return 'stop', 'wake' (early interrupt), or 'done' (full elapsed)."""
        remaining = float(seconds)
        while remaining > 0:
            if self._stop_event.is_set():
                return "stop"
            if self._wake_event.is_set():
                self._wake_event.clear()
                return "wake"
            chunk = min(remaining, 1.0)
            if self._stop_event.wait(timeout=chunk):
                return "stop"
            if self._wake_event.is_set():
                self._wake_event.clear()
                return "wake"
            remaining -= chunk
        return "done"

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.cfg.scheduler_enabled:
                if self._interruptible_wait(1.0):
                    break
                continue
            self._execute_folder_batch(label="scheduled")
            if self._stop_event.is_set():
                break
            while self.cfg.scheduler_enabled and not self._stop_event.is_set():
                with self._cfg_lock:
                    wait_sec = wait_seconds_after_run(self.cfg)
                why = self._wait_interval(wait_sec)
                if why == "stop":
                    return
                if not self.cfg.scheduler_enabled:
                    break
                if why == "wake":
                    continue
                self._execute_folder_batch(label="scheduled")
                if self._stop_event.is_set():
                    break

    def _execute_folder_batch(self, label: str) -> None:
        try:
            mp = int(self.max_parallel_var.get())
        except (tk.TclError, ValueError):
            mp = None
        with self._cfg_lock:
            path = self.config_path
        cfg = load_config(path)

        def log(msg: str) -> None:
            self._log_queue.put(msg)

        run_enabled_folders(cfg, path, max_parallel=mp, log=log, label=label)
        with self._cfg_lock:
            self.cfg = load_config(self.config_path)
        self._queue_ui(self._refresh_tree)

    def _queue_ui(self, fn: Any) -> None:
        try:
            self.root.after(0, fn)
        except tk.TclError:
            pass

    def _save_quiet(self) -> None:
        self._sync_schedule_timing_to_cfg()
        self._sync_parallel_to_cfg()
        self.cfg.scheduler_enabled = bool(self.scheduler_var.get())
        try:
            save_config(self.config_path, self.cfg)
        except OSError as e:
            self._append_log(f"Could not save config: {e}\n")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_log_queue)

    def _append_log(self, s: str) -> None:
        self.out.insert("end", s)
        self.out.see("end")

    def _refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._tree_item_to_index.clear()
        for idx, job in enumerate(self.cfg.folders):
            iid = self.tree.insert("", "end", values=self._row_values(job))
            self._tree_item_to_index[iid] = idx

    def _refresh_tree_row(self, index: int) -> None:
        for iid, i in self._tree_item_to_index.items():
            if i == index:
                job = self.cfg.folders[index]
                self.tree.item(iid, values=self._row_values(job))
                break

    def _row_values(self, job: FolderJob) -> tuple:
        on = "Yes" if job.enabled else "No"
        last = job.last_run or "—"
        if len(last) > 22:
            last = last[:19] + "…"
        path_disp = job.path
        if len(path_disp) > 70:
            path_disp = "…" + path_disp[-67:]
        return (on, path_disp, last)

    def _on_tree_select(self, _event: Any = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self._tree_item_to_index.get(sel[0])
        if idx is None or not (0 <= idx < len(self.cfg.folders)):
            return
        job = self.cfg.folders[idx]
        self.enabled_var.set(job.enabled)
        self.recursive_var.set(job.recursive)
        self.strategy_var.set(job.strategy)
        self.normalize_var.set(
            job.normalize if job.normalize not in (None, "") else effective_normalize(job)
        )
        self.hidden_var.set(job.include_hidden)
        self.collect_empty_var.set(job.collect_empty_dirs)

    def _push_detail_to_job(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self._tree_item_to_index.get(sel[0])
        if idx is None or not (0 <= idx < len(self.cfg.folders)):
            return
        job = self.cfg.folders[idx]
        job.enabled = bool(self.enabled_var.get())
        job.recursive = bool(self.recursive_var.get())
        job.strategy = self.strategy_var.get()
        r = job.recursive
        ui = self.normalize_var.get()
        job.normalize = None if ui == ("standard" if r else "none") else ui
        job.include_hidden = bool(self.hidden_var.get())
        job.collect_empty_dirs = bool(self.collect_empty_var.get())
        self._refresh_tree_row(idx)

    def _add_folder(self) -> None:
        d = filedialog.askdirectory(title="Add folder to schedule")
        if not d:
            return
        path = str(Path(d).expanduser())
        for existing in self.cfg.folders:
            if Path(existing.path).expanduser().resolve() == Path(path).expanduser().resolve():
                messagebox.showinfo("Folder", "That folder is already in the list.")
                return
        job = FolderJob(path=path)
        ok, preview = run_dry_run_preview(job)
        if not ok:
            if not messagebox.askyesno("Dry run failed", f"{preview}\n\nAdd anyway?"):
                return
            job.dry_run_verified = True
        else:
            job.dry_run_verified = True
        with self._cfg_lock:
            self.cfg.folders.append(job)
        self._append_log(f"Added {path} (dry-run preview: {ok})\n")
        self._refresh_tree()
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[-1])
            self.tree.see(children[-1])
        self._on_tree_select()
        self._save_quiet()

    def _remove_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Remove", "Select a folder first.")
            return
        idx = self._tree_item_to_index.get(sel[0])
        if idx is None:
            return
        with self._cfg_lock:
            del self.cfg.folders[idx]
        self._refresh_tree()
        self._save_quiet()

    def _run_selected_now(self) -> None:
        helper = organizer_script_path()
        if not helper.is_file():
            messagebox.showerror("Missing script", f"Could not find:\n{helper}")
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Run", "Select a folder first.")
            return
        idx = self._tree_item_to_index.get(sel[0])
        if idx is None or not (0 <= idx < len(self.cfg.folders)):
            return
        job = self.cfg.folders[idx]
        base = Path(job.path).expanduser()
        if not base.is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{base}")
            return
        cmd = build_organize_cmd(job)
        path_key = str(Path(job.path).expanduser().resolve())
        self._append_log("\n--- manual run (one folder) ---\n$ " + " ".join(cmd) + "\n\n")

        def worker() -> None:
            def log(msg: str) -> None:
                self._log_queue.put(msg)

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            except subprocess.TimeoutExpired:
                log("Timed out after 1 hour.\n")
                return
            except OSError as e:
                log(f"Could not run helper: {e}\n")
                return
            err = (proc.stderr or "").strip()
            out = (proc.stdout or "").strip()
            if err:
                log(err + "\n")
            if out:
                log(out + "\n")
            if proc.returncode != 0:
                log(f"\n(exit code {proc.returncode})\n")
                return

            def apply_success() -> None:
                idx2 = None
                for i, j in enumerate(self.cfg.folders):
                    try:
                        if str(Path(j.path).expanduser().resolve()) == path_key:
                            idx2 = i
                            break
                    except OSError:
                        continue
                if idx2 is None:
                    return
                self.cfg.folders[idx2].last_run = datetime.now(timezone.utc).isoformat()
                self.cfg.folders[idx2].last_error = None
                self._refresh_tree_row(idx2)
                self._save_quiet()

            self._queue_ui(apply_success)

        threading.Thread(target=worker, daemon=True).start()

    def _run_all_enabled_now(self) -> None:
        helper = organizer_script_path()
        if not helper.is_file():
            messagebox.showerror("Missing script", f"Could not find:\n{helper}")
            return

        def worker() -> None:
            try:
                mp = int(self.max_parallel_var.get())
            except (tk.TclError, ValueError):
                mp = None

            def log(msg: str) -> None:
                self._log_queue.put(msg)

            with self._cfg_lock:
                path = self.config_path
            cfg = load_config(path)
            run_enabled_folders(cfg, path, max_parallel=mp, log=log, label="manual all enabled")
            with self._cfg_lock:
                self.cfg = load_config(self.config_path)
            self._queue_ui(self._refresh_tree)

        self._append_log("\n--- Run all enabled (parallel) ---\n")
        threading.Thread(target=worker, daemon=True).start()

    def _save(self) -> None:
        self._sync_schedule_timing_to_cfg()
        self._sync_parallel_to_cfg()
        self.cfg.scheduler_enabled = bool(self.scheduler_var.get())
        try:
            save_config(self.config_path, self.cfg)
        except OSError as e:
            messagebox.showerror("Save", str(e))
            return
        self._append_log(f"Saved: {self.config_path}\n")

    def _on_close(self) -> None:
        self._stop_event.set()
        self._sync_schedule_timing_to_cfg()
        self._sync_parallel_to_cfg()
        self.cfg.scheduler_enabled = bool(self.scheduler_var.get())
        try:
            save_config(self.config_path, self.cfg)
        except OSError:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ScheduleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
