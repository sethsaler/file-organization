#!/usr/bin/env python3
"""Tk UI: organize folders by type, with an integrated Schedule tab."""

from __future__ import annotations

import json
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_manifest import list_manifests
from schedule_panel import SchedulePanel


def _helper_script() -> Path:
    return _SCRIPT_DIR / "organize_by_filetype.py"


def _restore_script() -> Path:
    return _SCRIPT_DIR / "restore_from_backup.py"


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

        notebook = ttk.Notebook(root)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook = notebook

        organize_tab = ttk.Frame(notebook, padding=10)
        schedule_tab = ttk.Frame(notebook)
        notebook.add(organize_tab, text="Organize")
        notebook.add(schedule_tab, text="Schedule")
        schedule_tab.columnconfigure(0, weight=1)
        schedule_tab.rowconfigure(0, weight=1)

        self._build_organize_tab(organize_tab)

        self.schedule_panel = SchedulePanel(schedule_tab, root, embedded=True)
        self.schedule_panel.grid(row=0, column=0, sticky="nsew")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if not _helper_script().is_file():
            self._append_text(f"Missing helper:\n{_helper_script()}\n")

    def _build_organize_tab(self, frm: ttk.Frame) -> None:
        pad = {"padx": 8, "pady": 4}
        row = 0
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Folder:").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.path_var).grid(row=row, column=1, sticky="ew", **pad)
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

        for var, label in [
            (self.hidden_var, "Include hidden files and folders"),
            (self.collect_empty_var, "Collect empty folders into “For Deletion”"),
            (self.exclude_defaults_var, "Exclude .git, node_modules, venv, …"),
            (self.verbose_var, "Verbose progress (stderr)"),
            (self.mime_var, "MIME-sniff extensionless files"),
        ]:
            ttk.Checkbutton(frm, text=label, variable=var).grid(row=row, column=0, columnspan=3, sticky="w", **pad)
            row += 1

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Button(btn_row, text="Dry run", command=lambda: self._run(dry_run=True)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Run", command=lambda: self._run(dry_run=False)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Restore…", command=self._restore).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Add to schedule…", command=self._add_to_schedule).pack(side="left")
        row += 1

        ttk.Label(frm, text="Output (JSON or errors):").grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1
        self.out = scrolledtext.ScrolledText(
            frm,
            height=14,
            wrap="word",
            font=("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10),
        )
        self.out.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(row, weight=1)

    def _on_close(self) -> None:
        self.schedule_panel.shutdown()
        self.root.destroy()

    def _browse(self) -> None:
        d = filedialog.askdirectory(title="Choose folder to organize")
        if d:
            self.path_var.set(d)

    def _append_text(self, s: str) -> None:
        self.out.insert("end", s)
        self.out.see("end")

    def _effective_normalize(self) -> str | None:
        recursive = self.recursive_var.get()
        norm_ui = self.normalize_var.get()
        eff_norm = "standard" if recursive else "none"
        return None if norm_ui == eff_norm else norm_ui

    def _add_to_schedule(self) -> None:
        raw = self.path_var.get().strip()
        if not raw:
            messagebox.showwarning("Folder", "Choose a folder on the Organize tab first.")
            return
        if not Path(raw).expanduser().is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{raw}")
            return
        self.schedule_panel.add_folder_path(
            raw,
            recursive=self.recursive_var.get(),
            strategy=self.strategy_var.get(),
            normalize=self._effective_normalize(),
            include_hidden=self.hidden_var.get(),
            collect_empty_dirs=self.collect_empty_var.get(),
        )
        self.notebook.select(1)

    def _build_cmd(self, dry_run: bool) -> list[str]:
        base = Path(self.path_var.get().strip()).expanduser()
        recursive = self.recursive_var.get()
        norm_ui = self.normalize_var.get()
        eff_norm = "standard" if recursive else "none"
        cmd = [
            sys.executable,
            str(_helper_script()),
            "--path",
            str(base),
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
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run(self, dry_run: bool) -> None:
        raw = self.path_var.get().strip()
        if not raw:
            messagebox.showwarning("Folder", "Choose a folder first.")
            return
        if not Path(raw).expanduser().is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{raw}")
            return
        cmd = self._build_cmd(dry_run)
        self._append_text("\n---\n$ " + " ".join(cmd) + "\n\n")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            self._append_text("Timed out after 1 hour.\n")
            return
        except OSError as e:
            self._append_text(f"Could not run: {e}\n")
            return
        if proc.stderr:
            self._append_text(proc.stderr + "\n")
        if proc.stdout:
            try:
                self._append_text(json.dumps(json.loads(proc.stdout), indent=2) + "\n")
            except json.JSONDecodeError:
                self._append_text(proc.stdout + "\n")
        if proc.returncode != 0:
            self._append_text(f"(exit {proc.returncode})\n")

    def _restore(self) -> None:
        raw = self.path_var.get().strip()
        if not raw:
            messagebox.showwarning("Restore", "Choose the folder that was organized.")
            return
        base = Path(raw).expanduser()
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
        self._append_text("\n--- restore ---\n$ " + " ".join(cmd) + "\n\n")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            self._append_text(proc.stdout + "\n")
        if proc.stderr:
            self._append_text(proc.stderr + "\n")


def main() -> None:
    root = tk.Tk()
    TinkerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
