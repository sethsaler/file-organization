#!/usr/bin/env python3
"""Compatibility entrypoint for the native File Organizer command center."""

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


_OUTPUT_FONT = ("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10)


class CollapsibleFrame(ttk.Frame):
    """A frame with a toggle button that shows or hides its child frame."""

    def __init__(self, parent: tk.Widget, title: str, *, expanded: bool = False, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._title = title
        self._expanded = expanded
        self._button = ttk.Button(self, text=self._button_text(), command=self._toggle)
        self._button.pack(fill="x")
        self.content = ttk.Frame(self)
        if expanded:
            self.content.pack(fill="x", expand=True)

    def _button_text(self) -> str:
        return f"Hide {self._title}" if self._expanded else f"Show {self._title}"

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._button.configure(text=self._button_text())
        if self._expanded:
            self.content.pack(fill="x", expand=True)
        else:
            self.content.pack_forget()


class TinkerApp:
    """Simple Tkinter front end for the organizer and random-renamer scripts."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Organize by file type")
        self.root.minsize(640, 600)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Organize tab state
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
        self.rename_after_organize_var = tk.BooleanVar(value=False)
        self.skip_randomly_renamed_var = tk.BooleanVar(value=True)
        self.expand_subfolders_var = tk.BooleanVar(value=False)

        # Random rename tab state
        self.rename_path_var = tk.StringVar()
        self.rename_recursive_var = tk.BooleanVar(value=True)
        self.rename_hidden_var = tk.BooleanVar(value=True)
        self.rename_verbose_var = tk.BooleanVar(value=False)
        self.rename_skip_randomly_renamed_var = tk.BooleanVar(value=True)

        # Background worker state
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

        organize_tab = ttk.Frame(notebook)
        schedule_tab = ttk.Frame(notebook)
        rename_tab = ttk.Frame(notebook)
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
            self._append_to("organize", f"Missing helper:\n{_helper_script()}\n")
        if not _rename_script().is_file():
            self._append_to("organize", f"Missing rename script:\n{_rename_script()}\n")

    # --------------------------------------------------------------------- #
    # Common UI builders
    # --------------------------------------------------------------------- #

    def _make_path_row(self, parent: ttk.Frame, var: tk.StringVar, title: str) -> tk.Entry:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="Folder:").pack(side="left")
        entry = tk.Entry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._bind_path_entry_clipboard(entry)
        ttk.Button(row, text="Browse…", command=lambda: self._browse(var, title)).pack(side="left")
        return entry

    def _make_output(self, parent: ttk.Frame) -> scrolledtext.ScrolledText:
        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(8, 0))
        ttk.Label(header, text="Output:").pack(side="left")
        ttk.Label(header, textvariable=self.status_var, foreground="gray").pack(side="right")
        out = scrolledtext.ScrolledText(parent, height=14, wrap="word", font=_OUTPUT_FONT)
        out.pack(fill="both", expand=True, pady=(4, 0))
        return out

    def _make_label_frame(self, parent: ttk.Frame, title: str) -> ttk.LabelFrame:
        group = ttk.LabelFrame(parent, text=title, padding=6)
        group.pack(fill="x", pady=4)
        return group

    def _make_check(self, parent: ttk.Widget, text: str, variable: tk.BooleanVar, **kwargs) -> ttk.Checkbutton:
        btn = ttk.Checkbutton(parent, text=text, variable=variable)
        btn.pack(anchor="w", **kwargs)
        return btn

    def _make_radio(self, parent: ttk.Widget, text: str, variable: tk.StringVar, value: str, **kwargs) -> ttk.Radiobutton:
        btn = ttk.Radiobutton(parent, text=text, variable=variable, value=value)
        btn.pack(anchor="w", **kwargs)
        return btn

    def _make_button_row(self, parent: ttk.Frame, buttons: list[tuple[str, object, str, str | None, dict | None]]) -> None:
        """Pack a row of buttons.

        Each entry is (text, command, side, kind, pack_options):
          - kind: "run" (disabled while busy), "cancel" (enabled while busy), or None.
          - pack_options: extra kwargs for pack, or None.
        """
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        for text, command, side, kind, pack_opts in buttons:
            pack_opts = pack_opts or {}
            btn = ttk.Button(row, text=text, command=command)
            btn.pack(side=side, **pack_opts)
            if kind == "run":
                self._run_buttons.append(btn)
            elif kind == "cancel":
                self._cancel_buttons.append(btn)

    def _make_collapsible(self, parent: ttk.Frame, title: str, *, expanded: bool = False) -> ttk.Frame:
        frame = CollapsibleFrame(parent, title, expanded=expanded)
        frame.pack(fill="x", pady=4)
        return frame.content

    # --------------------------------------------------------------------- #
    # Tabs
    # --------------------------------------------------------------------- #

    def _build_organize_tab(self, tab: ttk.Frame) -> None:
        frm = ttk.Frame(tab, padding=10)
        frm.pack(fill="both", expand=True)

        self.path_entry = self._make_path_row(frm, self.path_var, "Choose folder to organize")
        self._make_check(frm, "Recursive", self.recursive_var)

        group = self._make_label_frame(frm, "Profile")
        ttk.Combobox(
            group,
            textvariable=self.profile_var,
            values=("standard", "extended"),
            state="readonly",
            width=24,
        ).pack(anchor="w")

        group = self._make_label_frame(frm, "Recursive strategy")
        self._make_radio(group, "Flatten to root buckets", self.strategy_var, "flatten-root")
        self._make_radio(group, "In-place", self.strategy_var, "in-place")

        group = self._make_label_frame(frm, "Normalization")
        self._make_radio(group, "Standard", self.normalize_var, "standard")
        self._make_radio(group, "None", self.normalize_var, "none")

        advanced = self._make_collapsible(frm, "Advanced options")

        group = self._make_label_frame(advanced, "File handling")
        self._make_check(group, "Include hidden files and folders", self.hidden_var)
        self._make_check(group, "Collect empty folders into “For Deletion”", self.collect_empty_var)
        self._make_check(group, "Exclude .git, node_modules, venv, …", self.exclude_defaults_var)
        self._make_check(group, "Detect duplicates (identical content) into “Duplicates”", self.detect_duplicates_var)
        self._make_check(group, "…keep duplicates in place as hardlinks (no extra disk space)", self.duplicates_hardlink_var, padx=(18, 0))
        self._make_check(group, "Sort into Year/Month subfolders inside each bucket", self.date_buckets_var)

        group = self._make_label_frame(advanced, "Random rename (opt-in)")
        self._make_check(group, "Rename all files with random names after organizing", self.rename_after_organize_var)
        self._make_check(group, "Skip already-randomly-renamed files (16-char names)", self.skip_randomly_renamed_var)

        group = self._make_label_frame(advanced, "Advanced")
        self._make_check(group, "Verbose progress (stderr)", self.verbose_var)
        self._make_check(group, "MIME-sniff extensionless files", self.mime_var)
        self._make_check(group, "Expand to organize each subfolder separately", self.expand_subfolders_var)

        self._make_button_row(
            frm,
            [
                ("Dry run", lambda: self._run_organize(True), "left", "run", {"padx": (0, 6)}),
                ("Run", lambda: self._run_organize(False), "left", "run", {"padx": (0, 6)}),
                ("Restore…", self._restore, "left", "run", {"padx": (0, 6)}),
                ("Add to schedule…", self._add_to_schedule, "left", None, {}),
                ("Cancel", self._cancel, "right", "cancel", {}),
                ("Clear", lambda: self.out.delete("1.0", "end"), "right", None, {"padx": (0, 6)}),
            ],
        )

        self.out = self._make_output(frm)

    def _build_rename_tab(self, tab: ttk.Frame) -> None:
        frm = ttk.Frame(tab, padding=10)
        frm.pack(fill="both", expand=True)

        self.rename_path_entry = self._make_path_row(frm, self.rename_path_var, "Choose folder for random renaming")
        self._make_check(frm, "Recursive (all subfolders)", self.rename_recursive_var)
        self._make_check(frm, "Include hidden files", self.rename_hidden_var)

        advanced = self._make_collapsible(frm, "Advanced options")
        self._make_check(advanced, "Verbose progress", self.rename_verbose_var)
        self._make_check(advanced, "Skip already-randomly-renamed files (16-char names)", self.rename_skip_randomly_renamed_var)

        self._make_button_row(
            frm,
            [
                ("Dry run", lambda: self._run_rename(True), "left", "run", {"padx": (0, 6)}),
                ("Run", lambda: self._run_rename(False), "left", "run", {}),
                ("Cancel", self._cancel, "right", "cancel", {}),
                ("Clear", lambda: self.rename_out.delete("1.0", "end"), "right", None, {"padx": (0, 6)}),
            ],
        )

        self.rename_out = self._make_output(frm)

    # --------------------------------------------------------------------- #
    # Event handlers
    # --------------------------------------------------------------------- #

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

    def _browse(self, var: tk.StringVar, title: str) -> None:
        d = filedialog.askdirectory(title=title)
        if d:
            var.set(d)

    def _bind_path_entry_clipboard(self, entry: tk.Entry) -> None:
        """Cmd+V fallback for macOS when the default Entry paste binding fails."""
        if sys.platform == "darwin":
            entry.bind("<Command-v>", self._on_path_paste)
            entry.bind("<Command-V>", self._on_path_paste)

    def _on_path_paste(self, event: tk.Event) -> str:
        entry = event.widget
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

    # --------------------------------------------------------------------- #
    # Background command execution
    # --------------------------------------------------------------------- #

    def _append_to(self, target: str, s: str) -> None:
        out = self.rename_out if target == "rename" else self.out
        out.insert("end", s)
        out.see("end")

    def _resolve_path(self, var: tk.StringVar) -> Path | None:
        text = var.get().strip()
        if not text:
            return None
        return normalize_folder_input(text)

    def _show_resolved_path(self, var: tk.StringVar, path: Path) -> None:
        resolved = str(path)
        if var.get() != resolved:
            var.set(resolved)

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
                self._append_to(target, text)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # --------------------------------------------------------------------- #
    # Commands
    # --------------------------------------------------------------------- #

    @staticmethod
    def _flag(variable: tk.BooleanVar, true_flag: str, false_flag: str | None = None) -> list[str]:
        if variable.get():
            return [true_flag]
        return [false_flag] if false_flag is not None else []

    def _effective_normalize(self) -> str | None:
        recursive = self.recursive_var.get()
        norm_ui = self.normalize_var.get()
        eff_norm = "standard" if recursive else "none"
        return None if norm_ui == eff_norm else norm_ui

    def _ensure_folder(self, var: tk.StringVar) -> Path | None:
        path = self._resolve_path(var)
        if path is None:
            messagebox.showwarning("Folder", "Choose a folder first.")
            return None
        if not path.is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{path}")
            return None
        self._show_resolved_path(var, path)
        return path

    def _add_to_schedule(self) -> None:
        path = self._resolve_path(self.path_var)
        if path is None:
            messagebox.showwarning("Folder", "Choose a folder on the Organize tab first.")
            return
        if not path.is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{path}")
            return
        self._show_resolved_path(self.path_var, path)
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

    def _build_organize_cmd(self, dry_run: bool, *, base: Path | None = None) -> list[str]:
        folder = base or self._resolve_path(self.path_var) or Path()
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
        cmd.extend(self._flag(self.recursive_var, "--recursive", "--no-recursive"))
        cmd.extend(self._flag(self.hidden_var, "--include-hidden", "--no-include-hidden"))
        cmd.extend(self._flag(self.collect_empty_var, "--collect-empty-dirs", "--no-collect-empty-dirs"))
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
        cmd.extend(self._flag(self.skip_randomly_renamed_var, "--skip-randomly-renamed", "--no-skip-randomly-renamed"))
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run_organize(self, dry_run: bool) -> None:
        path = self._ensure_folder(self.path_var)
        if path is None:
            return

        if self.expand_subfolders_var.get():
            subfolders = [item for item in path.iterdir() if item.is_dir()]
            if not subfolders:
                self._append_to("organize", "No subfolders found to organize.\n")
                return
            self._append_to("organize", f"Organizing {len(subfolders)} subfolder(s) separately:\n")
            jobs = [
                (f"\n--- {sub.name} ---\n", self._build_organize_cmd(dry_run, base=sub))
                for sub in subfolders
            ]
            self._start_jobs(jobs, "organize")
            return

        cmd = self._build_organize_cmd(dry_run)
        self._start_jobs([("\n---\n", cmd)], "organize")

    def _restore(self) -> None:
        base = self._resolve_path(self.path_var)
        if base is None:
            messagebox.showwarning("Restore", "Choose the folder that was organized.")
            return
        if not base.is_dir():
            messagebox.showerror("Restore", f"Not a directory:\n{base}")
            return
        self._show_resolved_path(self.path_var, base)
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

    def _build_rename_cmd(self, dry_run: bool, path: Path) -> list[str]:
        cmd = [
            sys.executable,
            str(_rename_script()),
            "--path",
            str(path),
        ]
        cmd.extend(self._flag(self.rename_recursive_var, "--recursive", "--no-recursive"))
        cmd.extend(self._flag(self.rename_hidden_var, "--include-hidden", "--no-include-hidden"))
        if self.rename_verbose_var.get():
            cmd.append("--verbose")
        if self.rename_skip_randomly_renamed_var.get():
            cmd.append("--skip-randomly-renamed")
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run_rename(self, dry_run: bool) -> None:
        path = self._ensure_folder(self.rename_path_var)
        if path is None:
            return
        cmd = self._build_rename_cmd(dry_run, path)
        self._start_jobs([("\n---\n", cmd)], "rename")


def main() -> None:
    from command_center import main as command_center_main

    command_center_main()


if __name__ == "__main__":
    main()
