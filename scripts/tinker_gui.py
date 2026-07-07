#!/usr/bin/env python3
"""Tk UI: organize folders by type, with an integrated Schedule tab."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_manifest import list_manifests
from org_paths import normalize_folder_input
from schedule_panel import SchedulePanel


def _helper_script() -> Path:
    return _SCRIPT_DIR / "organize_by_filetype.py"


def _restore_script() -> Path:
    return _SCRIPT_DIR / "restore_from_backup.py"


def _rename_script() -> Path:
    return _SCRIPT_DIR / "rename_files_randomly.py"


class TinkerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Organize by file type")
        self.root.minsize(640, 600)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.path_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.strategy_var = tk.StringVar(value="flatten-root")
        self.normalize_var = tk.StringVar(value="standard")
        self.profile_var = tk.StringVar(value="standard")
        self.hidden_var = tk.BooleanVar(value=True)
        self.collect_empty_var = tk.BooleanVar(value=True)
        self.exclude_defaults_var = tk.BooleanVar(value=True)
        self.verbose_var = tk.BooleanVar(value=False)
        self.mime_var = tk.BooleanVar(value=False)
        self.detect_duplicates_var = tk.BooleanVar(value=False)
        self.duplicates_hardlink_var = tk.BooleanVar(value=False)
        self.date_buckets_var = tk.BooleanVar(value=False)
        self.rename_after_organize_var = tk.BooleanVar(value=True)
        self.skip_randomly_renamed_var = tk.BooleanVar(value=True)
        self.expand_subfolders_var = tk.BooleanVar(value=False)

        # Random rename tab variables
        self.rename_path_var = tk.StringVar()
        self.rename_recursive_var = tk.BooleanVar(value=True)
        self.rename_hidden_var = tk.BooleanVar(value=True)
        self.rename_verbose_var = tk.BooleanVar(value=False)
        self.rename_skip_randomly_renamed_var = tk.BooleanVar(value=True)

        # Background command execution state
        self._out_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._active_proc: subprocess.Popen | None = None
        self._cancel_requested = False
        self._run_buttons: list[ttk.Button] = []
        self._cancel_buttons: list[ttk.Button] = []
        self.status_var = tk.StringVar(value="")

        notebook = ttk.Notebook(root)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook = notebook

        organize_tab = ttk.Frame(notebook, padding=10)
        schedule_tab = ttk.Frame(notebook)
        rename_tab = ttk.Frame(notebook, padding=10)
        notebook.add(organize_tab, text="Organize")
        notebook.add(schedule_tab, text="Schedule")
        notebook.add(rename_tab, text="Random Rename")
        schedule_tab.columnconfigure(0, weight=1)
        schedule_tab.rowconfigure(0, weight=1)

        self._build_organize_tab(organize_tab)
        self._build_rename_tab(rename_tab)

        self.schedule_panel = SchedulePanel(schedule_tab, root, embedded=True)
        self.schedule_panel.grid(row=0, column=0, sticky="nsew")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if not _helper_script().is_file():
            self._append_text(f"Missing helper:\n{_helper_script()}\n")
        if not _rename_script().is_file():
            self._append_text(f"Missing rename script:\n{_rename_script()}\n")

    def _build_organize_tab(self, frm: ttk.Frame) -> None:
        pad = {"padx": 8, "pady": 4}
        row = 0
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Folder:").grid(row=row, column=0, sticky="w", **pad)
        self.path_entry = tk.Entry(frm, textvariable=self.path_var)
        self.path_entry.grid(row=row, column=1, sticky="ew", **pad)
        self._bind_path_entry_clipboard(self.path_entry)
        ttk.Button(frm, text="Browse…", command=self._browse).grid(row=row, column=2, **pad)
        row += 1

        ttk.Checkbutton(frm, text="Recursive", variable=self.recursive_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        prof = ttk.LabelFrame(frm, text="Profile", padding=6)
        prof.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Combobox(
            prof,
            textvariable=self.profile_var,
            values=("standard", "extended"),
            state="readonly",
            width=24,
        ).pack(anchor="w")
        row += 1

        strat = ttk.LabelFrame(frm, text="Recursive strategy", padding=6)
        strat.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Radiobutton(strat, text="Flatten to root buckets", variable=self.strategy_var, value="flatten-root").pack(anchor="w")
        ttk.Radiobutton(strat, text="In-place", variable=self.strategy_var, value="in-place").pack(anchor="w")
        row += 1

        norm = ttk.LabelFrame(frm, text="Normalization", padding=6)
        norm.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Radiobutton(norm, text="Standard", variable=self.normalize_var, value="standard").pack(anchor="w")
        ttk.Radiobutton(norm, text="None", variable=self.normalize_var, value="none").pack(anchor="w")
        row += 1

        # File handling options
        file_opts = ttk.LabelFrame(frm, text="File handling", padding=6)
        file_opts.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(file_opts, text="Include hidden files and folders", variable=self.hidden_var).pack(anchor="w")
        ttk.Checkbutton(file_opts, text="Collect empty folders into “For Deletion”", variable=self.collect_empty_var).pack(anchor="w")
        ttk.Checkbutton(file_opts, text="Exclude .git, node_modules, venv, …", variable=self.exclude_defaults_var).pack(anchor="w")
        ttk.Checkbutton(file_opts, text="Detect duplicates (identical content) into “Duplicates”", variable=self.detect_duplicates_var).pack(anchor="w")
        ttk.Checkbutton(file_opts, text="…keep duplicates in place as hardlinks (no extra disk space)", variable=self.duplicates_hardlink_var).pack(anchor="w", padx=(18, 0))
        ttk.Checkbutton(file_opts, text="Sort into Year/Month subfolders inside each bucket", variable=self.date_buckets_var).pack(anchor="w")
        row += 1

        # Random rename options
        rename_opts = ttk.LabelFrame(frm, text="Random rename (default: on)", padding=6)
        rename_opts.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(rename_opts, text="Rename all files with random names after organizing", variable=self.rename_after_organize_var).pack(anchor="w")
        ttk.Checkbutton(rename_opts, text="Skip already-randomly-renamed files (16-char names)", variable=self.skip_randomly_renamed_var).pack(anchor="w")
        row += 1

        # Advanced options
        adv_opts = ttk.LabelFrame(frm, text="Advanced", padding=6)
        adv_opts.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(adv_opts, text="Verbose progress (stderr)", variable=self.verbose_var).pack(anchor="w")
        ttk.Checkbutton(adv_opts, text="MIME-sniff extensionless files", variable=self.mime_var).pack(anchor="w")
        ttk.Checkbutton(adv_opts, text="Expand to organize each subfolder separately", variable=self.expand_subfolders_var).pack(anchor="w")
        row += 1

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        dry_btn = ttk.Button(btn_row, text="Dry run", command=lambda: self._run(dry_run=True))
        dry_btn.pack(side="left", padx=(0, 6))
        run_btn = ttk.Button(btn_row, text="Run", command=lambda: self._run(dry_run=False))
        run_btn.pack(side="left", padx=(0, 6))
        restore_btn = ttk.Button(btn_row, text="Restore…", command=self._restore)
        restore_btn.pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Add to schedule…", command=self._add_to_schedule).pack(side="left")
        cancel_btn = ttk.Button(btn_row, text="Cancel", command=self._cancel, state="disabled")
        cancel_btn.pack(side="right")
        ttk.Button(btn_row, text="Clear", command=lambda: self.out.delete("1.0", "end")).pack(side="right", padx=(0, 6))
        self._run_buttons.extend([dry_btn, run_btn, restore_btn])
        self._cancel_buttons.append(cancel_btn)
        row += 1

        out_header = ttk.Frame(frm)
        out_header.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(out_header, text="Output (JSON or errors):").pack(side="left")
        ttk.Label(out_header, textvariable=self.status_var, foreground="gray").pack(side="right")
        row += 1
        self.out = scrolledtext.ScrolledText(
            frm,
            height=14,
            wrap="word",
            font=("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10),
        )
        self.out.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(row, weight=1)

    def _build_rename_tab(self, frm: ttk.Frame) -> None:
        """Build the Random Rename tab UI."""
        pad = {"padx": 8, "pady": 4}
        row = 0
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Folder:").grid(row=row, column=0, sticky="w", **pad)
        self.rename_path_entry = tk.Entry(frm, textvariable=self.rename_path_var)
        self.rename_path_entry.grid(row=row, column=1, sticky="ew", **pad)
        self._bind_path_entry_clipboard(self.rename_path_entry)
        ttk.Button(frm, text="Browse…", command=self._rename_browse).grid(row=row, column=2, **pad)
        row += 1

        ttk.Checkbutton(frm, text="Recursive (all subfolders)", variable=self.rename_recursive_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        ttk.Checkbutton(frm, text="Include hidden files", variable=self.rename_hidden_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        ttk.Checkbutton(frm, text="Verbose progress", variable=self.rename_verbose_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        ttk.Checkbutton(frm, text="Skip already-randomly-renamed files (16-char names)", variable=self.rename_skip_randomly_renamed_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        dry_btn = ttk.Button(btn_row, text="Dry run", command=lambda: self._run_rename(dry_run=True))
        dry_btn.pack(side="left", padx=(0, 6))
        run_btn = ttk.Button(btn_row, text="Run", command=lambda: self._run_rename(dry_run=False))
        run_btn.pack(side="left")
        cancel_btn = ttk.Button(btn_row, text="Cancel", command=self._cancel, state="disabled")
        cancel_btn.pack(side="right")
        ttk.Button(btn_row, text="Clear", command=lambda: self.rename_out.delete("1.0", "end")).pack(side="right", padx=(0, 6))
        self._run_buttons.extend([dry_btn, run_btn])
        self._cancel_buttons.append(cancel_btn)
        row += 1

        ttk.Label(frm, text="Output:").grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        self.rename_out = scrolledtext.ScrolledText(
            frm,
            height=14,
            wrap="word",
            font=("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10),
        )
        self.rename_out.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(row, weight=1)

    def _on_close(self) -> None:
        self._cancel_requested = True
        proc = self._active_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.schedule_panel.shutdown()
        self.root.destroy()

    def _browse(self) -> None:
        d = filedialog.askdirectory(title="Choose folder to organize")
        if d:
            self.path_var.set(d)

    def _rename_browse(self) -> None:
        d = filedialog.askdirectory(title="Choose folder for random renaming")
        if d:
            self.rename_path_var.set(d)

    def _bind_path_entry_clipboard(self, entry: tk.Entry) -> None:
        """Cmd+V fallback for macOS when the default Entry paste binding fails."""
        if sys.platform == "darwin":
            entry.bind("<Command-v>", self._on_path_paste)
            entry.bind("<Command-V>", self._on_path_paste)

    def _on_path_paste(self, event: tk.Event | None = None) -> str:
        entry = event.widget if event is not None else self.path_entry
        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            if entry.selection_present():
                entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            pass
        entry.insert(tk.INSERT, clip)
        return "break"

    def _append_text(self, s: str) -> None:
        self.out.insert("end", s)
        self.out.see("end")

    def _append_rename_text(self, s: str) -> None:
        self.rename_out.insert("end", s)
        self.rename_out.see("end")

    def _parse_folder_path(self, raw: str | None = None) -> Path | None:
        text = self.path_var.get() if raw is None else raw
        if not text.strip():
            return None
        return normalize_folder_input(text)

    def _show_resolved_path(self, path: Path) -> None:
        resolved = str(path)
        if self.path_var.get() != resolved:
            self.path_var.set(resolved)

    # --- Background command execution -------------------------------------

    def _is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in self._run_buttons:
            btn.configure(state=state)
        for btn in self._cancel_buttons:
            btn.configure(state="normal" if busy else "disabled")
        self.status_var.set("Running…" if busy else "")

    def _cancel(self) -> None:
        self._cancel_requested = True
        proc = self._active_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.status_var.set("Cancelling…")

    def _start_jobs(self, jobs: list[tuple[str, list[str]]], target: str) -> None:
        """Run *jobs* (header, cmd) sequentially in a worker thread.

        *target* is "organize" or "rename" and selects the output pane.
        """
        if self._is_running():
            messagebox.showwarning("Busy", "A command is already running.")
            return
        self._cancel_requested = False
        self._set_busy(True)
        self._worker = threading.Thread(target=self._worker_main, args=(jobs, target), daemon=True)
        self._worker.start()
        self.root.after(100, self._poll_queue)

    def _worker_main(self, jobs: list[tuple[str, list[str]]], target: str) -> None:
        for header, cmd in jobs:
            if self._cancel_requested:
                self._out_queue.put((target, "Cancelled.\n"))
                break
            self._out_queue.put((target, header + "$ " + " ".join(cmd) + "\n\n"))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except OSError as e:
                self._out_queue.put((target, f"Could not run: {e}\n"))
                continue
            self._active_proc = proc
            try:
                stdout, stderr = proc.communicate(timeout=3600)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                self._out_queue.put((target, "Timed out after 1 hour.\n"))
            finally:
                self._active_proc = None
            self._out_queue.put((target, self._format_result(stdout, stderr, proc.returncode)))
        self._out_queue.put(None)

    @staticmethod
    def _format_result(stdout: str, stderr: str, returncode: int) -> str:
        parts: list[str] = []
        if stderr:
            parts.append(stderr + "\n")
        if stdout:
            try:
                parts.append(json.dumps(json.loads(stdout), indent=2) + "\n")
            except json.JSONDecodeError:
                parts.append(stdout + "\n")
        if returncode != 0:
            parts.append(f"(exit {returncode})\n")
        return "".join(parts)

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._out_queue.get_nowait()
                if item is None:
                    self._set_busy(False)
                    return
                target, text = item
                if target == "rename":
                    self._append_rename_text(text)
                else:
                    self._append_text(text)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _effective_normalize(self) -> str | None:
        recursive = self.recursive_var.get()
        norm_ui = self.normalize_var.get()
        eff_norm = "standard" if recursive else "none"
        return None if norm_ui == eff_norm else norm_ui

    def _add_to_schedule(self) -> None:
        path = self._parse_folder_path()
        if path is None:
            messagebox.showwarning("Folder", "Choose a folder on the Organize tab first.")
            return
        if not path.is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{path}")
            return
        self._show_resolved_path(path)
        self.schedule_panel.add_folder_path(
            str(path),
            recursive=self.recursive_var.get(),
            strategy=self.strategy_var.get(),
            normalize=self._effective_normalize(),
            include_hidden=self.hidden_var.get(),
            collect_empty_dirs=self.collect_empty_var.get(),
            profile=self.profile_var.get(),
            exclude_defaults=self.exclude_defaults_var.get(),
            expand_subfolders=self.expand_subfolders_var.get(),
            random_names_after_organize=self.rename_after_organize_var.get(),
            skip_randomly_renamed=self.skip_randomly_renamed_var.get(),
            detect_duplicates=self.detect_duplicates_var.get(),
            duplicates_hardlink=self.duplicates_hardlink_var.get(),
            date_buckets=self.date_buckets_var.get(),
        )
        self.notebook.select(1)

    def _build_cmd(self, dry_run: bool, *, base: Path | None = None) -> list[str]:
        folder = base or self._parse_folder_path() or Path()
        recursive = self.recursive_var.get()
        norm_ui = self.normalize_var.get()
        eff_norm = "standard" if recursive else "none"
        cmd = [
            sys.executable,
            str(_helper_script()),
            "--path",
            str(folder),
            "--strategy",
            self.strategy_var.get(),
            "--profile",
            self.profile_var.get(),
        ]
        if norm_ui != eff_norm:
            cmd.extend(["--normalize", norm_ui])
        if recursive:
            cmd.append("--recursive")
        else:
            cmd.append("--no-recursive")
        if not self.hidden_var.get():
            cmd.append("--no-include-hidden")
        if self.collect_empty_var.get():
            cmd.append("--collect-empty-dirs")
        else:
            cmd.append("--no-collect-empty-dirs")
        if self.exclude_defaults_var.get():
            cmd.append("--exclude-defaults")
        if self.verbose_var.get():
            cmd.append("--verbose")
        if self.mime_var.get():
            cmd.append("--mime-sniff")
        if self.detect_duplicates_var.get():
            cmd.append("--detect-duplicates")
            if self.duplicates_hardlink_var.get():
                cmd.append("--duplicates-hardlink")
        if self.date_buckets_var.get():
            cmd.append("--date-buckets")
        if self.rename_after_organize_var.get():
            cmd.append("--random-names-after-organize")
        if self.skip_randomly_renamed_var.get():
            cmd.append("--skip-randomly-renamed")
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run(self, dry_run: bool) -> None:
        path = self._parse_folder_path()
        if path is None:
            messagebox.showwarning("Folder", "Choose a folder first.")
            return
        if not path.is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{path}")
            return
        self._show_resolved_path(path)

        # Handle expand_subfolders
        if self.expand_subfolders_var.get():
            subfolders = [item for item in path.iterdir() if item.is_dir()]
            if not subfolders:
                self._append_text("No subfolders found to organize.\n")
                return
            self._append_text(f"Organizing {len(subfolders)} subfolder(s) separately:\n")
            jobs = [
                (f"\n--- {sub.name} ---\n", self._build_cmd(dry_run, base=sub))
                for sub in subfolders
            ]
            self._start_jobs(jobs, "organize")
            return

        # Normal single-folder run
        cmd = self._build_cmd(dry_run, base=path)
        self._start_jobs([("\n---\n", cmd)], "organize")

    def _restore(self) -> None:
        base = self._parse_folder_path()
        if base is None:
            messagebox.showwarning("Restore", "Choose the folder that was organized.")
            return
        self._show_resolved_path(base)
        manifests = list_manifests(base)
        if not manifests:
            messagebox.showinfo("Restore", "No backup manifests found in .organizer/")
            return
        manifest = manifests[0]
        if len(manifests) > 1:
            pick = messagebox.askyesno(
                "Restore",
                f"Restore from latest backup?\n{manifest.name}\n\nNo = pick another file",
            )
            if not pick:
                chosen = filedialog.askopenfilename(
                    title="Choose manifest",
                    initialdir=str(base / ".organizer"),
                    filetypes=[("JSON", "*.json")],
                )
                if not chosen:
                    return
                manifest = Path(chosen)
        if not messagebox.askyesno("Restore", f"Restore files from:\n{manifest}\n\nThis moves files back."):
            return
        cmd = [sys.executable, str(_restore_script()), str(manifest)]
        self._start_jobs([("\n--- restore ---\n", cmd)], "organize")

    def _run_rename(self, dry_run: bool) -> None:
        """Run the random rename operation."""
        path_str = self.rename_path_var.get()
        if not path_str.strip():
            messagebox.showwarning("Folder", "Choose a folder first.")
            return

        path = normalize_folder_input(path_str)
        if not path.is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{path}")
            return

        self.rename_path_var.set(str(path))

        cmd = [
            sys.executable,
            str(_rename_script()),
            "--path",
            str(path),
        ]

        if self.rename_recursive_var.get():
            cmd.append("--recursive")
        else:
            cmd.append("--no-recursive")

        if not self.rename_hidden_var.get():
            cmd.append("--no-include-hidden")

        if self.rename_verbose_var.get():
            cmd.append("--verbose")

        if self.rename_skip_randomly_renamed_var.get():
            cmd.append("--skip-randomly-renamed")

        if dry_run:
            cmd.append("--dry-run")

        self._start_jobs([("\n---\n", cmd)], "rename")


def main() -> None:
    root = tk.Tk()
    TinkerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
