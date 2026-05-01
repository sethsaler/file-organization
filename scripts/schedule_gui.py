#!/usr/bin/env python3
"""Tk GUI: designate folders to organize on a repeating interval while this window stays open.

Schedules are saved to a JSON file so your folder list and options persist. The scheduler
runs in a background thread and invokes organize_by_filetype.py (same as the Tinker GUI).

Keep this app running for automatic runs; closing the window stops the timer.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional


CONFIG_VERSION = 1


def _helper_script() -> Path:
    return Path(__file__).resolve().parent / "organize_by_filetype.py"


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        cfg = Path(base) / "file-organization"
    else:
        cfg = Path.home() / ".config" / "file-organization"
    return cfg / "schedule.json"


@dataclass
class FolderJob:
    path: str
    enabled: bool = True
    recursive: bool = True
    strategy: str = "flatten-root"
    normalize: str = "standard"
    include_hidden: bool = True
    collect_empty_dirs: bool = True
    last_run: Optional[str] = None
    last_error: Optional[str] = None


@dataclass
class ScheduleConfig:
    version: int = CONFIG_VERSION
    interval_minutes: int = 60
    scheduler_enabled: bool = False
    folders: List[FolderJob] = field(default_factory=list)

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "interval_minutes": self.interval_minutes,
            "scheduler_enabled": self.scheduler_enabled,
            "folders": [asdict(f) for f in self.folders],
        }

    @classmethod
    def from_json_dict(cls, data: Dict[str, Any]) -> ScheduleConfig:
        ver = int(data.get("version", 1))
        folders_raw = data.get("folders") or []
        folders: List[FolderJob] = []
        for item in folders_raw:
            if not isinstance(item, dict):
                continue
            p = str(item.get("path", "")).strip()
            if not p:
                continue
            folders.append(
                FolderJob(
                    path=p,
                    enabled=bool(item.get("enabled", True)),
                    recursive=bool(item.get("recursive", True)),
                    strategy=str(item.get("strategy", "flatten-root")),
                    normalize=str(item.get("normalize", "standard")),
                    include_hidden=bool(item.get("include_hidden", True)),
                    collect_empty_dirs=bool(item.get("collect_empty_dirs", True)),
                    last_run=item.get("last_run"),
                    last_error=item.get("last_error"),
                )
            )
        return cls(
            version=ver,
            interval_minutes=max(1, min(10080, int(data.get("interval_minutes", 60)))),
            scheduler_enabled=bool(data.get("scheduler_enabled", False)),
            folders=folders,
        )


def load_config(path: Path) -> ScheduleConfig:
    if not path.is_file():
        return ScheduleConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ScheduleConfig()
        return ScheduleConfig.from_json_dict(data)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return ScheduleConfig()


def save_config(path: Path, cfg: ScheduleConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_json_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def build_organize_cmd(job: FolderJob) -> List[str]:
    base = Path(job.path).expanduser()
    cmd = [
        sys.executable,
        str(_helper_script()),
        "--path",
        str(base),
        "--strategy",
        job.strategy,
        "--normalize",
        job.normalize,
    ]
    if job.recursive:
        cmd.append("--recursive")
    else:
        cmd.append("--no-recursive")
    if not job.include_hidden:
        cmd.append("--no-include-hidden")
    if job.collect_empty_dirs:
        cmd.append("--collect-empty-dirs")
    else:
        cmd.append("--no-collect-empty-dirs")
    return cmd


class ScheduleApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config_path = default_config_path()
        self.cfg = load_config(self.config_path)

        self.root.title("Organize on a schedule")
        self.root.minsize(640, 520)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._cfg_lock = threading.Lock()

        self.interval_var = tk.IntVar(value=self.cfg.interval_minutes)
        self.scheduler_var = tk.BooleanVar(value=self.cfg.scheduler_enabled)

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
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Run every").grid(row=0, column=0, sticky="w", padx=(0, 6))
        spin = tk.Spinbox(
            top,
            from_=1,
            to=10080,
            width=8,
            textvariable=self.interval_var,
            command=self._sync_interval_to_cfg,
        )
        spin.grid(row=0, column=1, sticky="w")
        ttk.Label(top, text="minutes (while this window is open)").grid(row=0, column=2, sticky="w", padx=(6, 0))

        ttk.Checkbutton(
            top,
            text="Enable automatic runs",
            variable=self.scheduler_var,
            command=self._on_scheduler_toggle,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

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
        ttk.Button(btn_row, text="Run selected now", command=self._run_selected_now).pack(side="left")

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

        helper = _helper_script()
        if not helper.is_file():
            self._append_log(f"Missing helper script:\n{helper}\n")

        if self.cfg.scheduler_enabled:
            self._ensure_worker()

    def _sync_interval_to_cfg(self) -> None:
        try:
            v = int(self.interval_var.get())
        except (tk.TclError, ValueError):
            v = 60
        v = max(1, min(10080, v))
        self.interval_var.set(v)
        self.cfg.interval_minutes = v

    def _on_scheduler_toggle(self) -> None:
        self.cfg.scheduler_enabled = bool(self.scheduler_var.get())
        if self.cfg.scheduler_enabled:
            self._ensure_worker()
            self._append_log("Automatic runs enabled (keep this window open).\n")
        else:
            self._append_log("Automatic runs disabled.\n")

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="schedule-worker", daemon=True)
        self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.scheduler_var.get():
                if self._stop_event.wait(timeout=1):
                    break
                continue
            try:
                minutes = max(1, min(10080, int(self.interval_var.get())))
            except (tk.TclError, ValueError):
                minutes = 60
            wait_sec = minutes * 60
            if self._stop_event.wait(timeout=wait_sec):
                break
            if not self.scheduler_var.get():
                continue
            self._run_scheduled_batch()

    def _run_scheduled_batch(self) -> None:
        with self._cfg_lock:
            snapshot = [(i, build_organize_cmd(j), j.path) for i, j in enumerate(self.cfg.folders) if j.enabled]

        for idx, cmd, _path in snapshot:
            with self._cfg_lock:
                if not (0 <= idx < len(self.cfg.folders)):
                    continue
                job = self.cfg.folders[idx]
                if not job.enabled:
                    continue
                base = Path(job.path).expanduser()
                if not base.is_dir():
                    job.last_error = "path missing or not a directory"
                    job.last_run = datetime.now(timezone.utc).isoformat()
                    self._queue_ui(lambda i=idx: self._after_job_update(i))
                    continue

            self._log_queue.put(f"\n--- scheduled ---\n$ {' '.join(cmd)}\n")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            except subprocess.TimeoutExpired:
                with self._cfg_lock:
                    if 0 <= idx < len(self.cfg.folders):
                        self.cfg.folders[idx].last_error = "timed out after 1 hour"
                        self.cfg.folders[idx].last_run = datetime.now(timezone.utc).isoformat()
                self._queue_ui(lambda i=idx: self._after_job_update(i))
                continue
            except OSError as e:
                with self._cfg_lock:
                    if 0 <= idx < len(self.cfg.folders):
                        self.cfg.folders[idx].last_error = str(e)
                        self.cfg.folders[idx].last_run = datetime.now(timezone.utc).isoformat()
                self._queue_ui(lambda i=idx: self._after_job_update(i))
                continue
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            with self._cfg_lock:
                if 0 <= idx < len(self.cfg.folders):
                    job = self.cfg.folders[idx]
                    if proc.returncode != 0:
                        job.last_error = err or f"exit {proc.returncode}"
                    else:
                        job.last_error = None
                    job.last_run = datetime.now(timezone.utc).isoformat()
            snippet = out if len(out) < 4000 else out[:4000] + "\n…(truncated)\n"
            if err:
                self._log_queue.put(err + "\n")
            if snippet:
                self._log_queue.put(snippet + "\n")
            self._queue_ui(lambda i=idx: self._after_job_update(i))

        self._queue_ui(self._save_quiet)

    def _queue_ui(self, fn: Any) -> None:
        try:
            self.root.after(0, fn)
        except tk.TclError:
            pass

    def _after_job_update(self, index: int) -> None:
        if 0 <= index < len(self.cfg.folders):
            self._refresh_tree_row(index)

    def _save_quiet(self) -> None:
        self._sync_interval_to_cfg()
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
        self.normalize_var.set(job.normalize)
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
        job.normalize = self.normalize_var.get()
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
        self.cfg.folders.append(FolderJob(path=path))
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
        del self.cfg.folders[idx]
        self._refresh_tree()
        self._save_quiet()

    def _run_selected_now(self) -> None:
        helper = _helper_script()
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
        self._append_log("\n--- manual run ---\n$ " + " ".join(cmd) + "\n\n")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            self._append_log("Timed out after 1 hour.\n")
            return
        except OSError as e:
            self._append_log(f"Could not run helper: {e}\n")
            return
        err = (proc.stderr or "").strip()
        out = (proc.stdout or "").strip()
        if err:
            self._append_log(err + "\n")
        if out:
            self._append_log(out + "\n")
        if proc.returncode != 0:
            self._append_log(f"\n(exit code {proc.returncode})\n")
        else:
            job.last_run = datetime.now(timezone.utc).isoformat()
            job.last_error = None
            self._refresh_tree_row(idx)
            self._save_quiet()

    def _save(self) -> None:
        self._sync_interval_to_cfg()
        self.cfg.scheduler_enabled = bool(self.scheduler_var.get())
        try:
            save_config(self.config_path, self.cfg)
        except OSError as e:
            messagebox.showerror("Save", str(e))
            return
        self._append_log(f"Saved: {self.config_path}\n")

    def _on_close(self) -> None:
        self._stop_event.set()
        self._sync_interval_to_cfg()
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
