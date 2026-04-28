#!/usr/bin/env python3
"""Simple Tk UI to run organize_by_filetype.py with visible options and JSON output."""

from __future__ import annotations

import json
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


def _helper_script() -> Path:
    return Path(__file__).resolve().parent / "organize_by_filetype.py"


class TinkerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Organize by file type — Tinker")
        self.root.minsize(520, 480)

        self.path_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.strategy_var = tk.StringVar(value="flatten-root")
        self.normalize_var = tk.StringVar(value="standard")
        self.hidden_var = tk.BooleanVar(value=False)
        self.collect_empty_var = tk.BooleanVar(value=True)

        pad = {"padx": 8, "pady": 4}
        row = 0

        frm = ttk.Frame(root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Folder:").grid(row=row, column=0, sticky="w", **pad)
        path_entry = ttk.Entry(frm, textvariable=self.path_var)
        path_entry.grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self._browse).grid(row=row, column=2, **pad)
        row += 1

        ttk.Checkbutton(frm, text="Recursive", variable=self.recursive_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        strat = ttk.LabelFrame(frm, text="Recursive strategy", padding=6)
        strat.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Radiobutton(strat, text="Flatten to root buckets (default)", variable=self.strategy_var, value="flatten-root").pack(
            anchor="w"
        )
        ttk.Radiobutton(strat, text="In-place (each folder sorts its own files)", variable=self.strategy_var, value="in-place").pack(anchor="w")
        row += 1

        norm = ttk.LabelFrame(frm, text="Normalization", padding=6)
        norm.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Radiobutton(norm, text="Standard (uppercase buckets, JPEG→JPG)", variable=self.normalize_var, value="standard").pack(
            anchor="w"
        )
        ttk.Radiobutton(norm, text="None", variable=self.normalize_var, value="none").pack(anchor="w")
        row += 1

        ttk.Checkbutton(frm, text="Include hidden files and folders", variable=self.hidden_var).grid(
            row=row, column=0, columnspan=3, sticky="w", **pad
        )
        row += 1

        ttk.Checkbutton(
            frm,
            text="Collect empty folders into “For Deletion”",
            variable=self.collect_empty_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Button(btn_row, text="Dry run", command=lambda: self._run(dry_run=True)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Run", command=lambda: self._run(dry_run=False)).pack(side="left")
        row += 1

        ttk.Label(frm, text="Output (JSON or errors):").grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        row += 1

        self.out = scrolledtext.ScrolledText(frm, height=16, wrap="word", font=("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10))
        self.out.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(row, weight=1)

        helper = _helper_script()
        if not helper.is_file():
            self._append_text(f"Missing helper script:\n{helper}\n\nClone or run install.sh first.\n")

    def _browse(self) -> None:
        d = filedialog.askdirectory(title="Choose folder to organize")
        if d:
            self.path_var.set(d)

    def _append_text(self, s: str) -> None:
        self.out.insert("end", s)
        self.out.see("end")

    def _build_cmd(self, dry_run: bool) -> list[str]:
        base = Path(self.path_var.get().strip()).expanduser()
        cmd = [
            sys.executable,
            str(_helper_script()),
            "--path",
            str(base),
            "--strategy",
            self.strategy_var.get(),
            "--normalize",
            self.normalize_var.get(),
        ]
        if self.recursive_var.get():
            cmd.append("--recursive")
        else:
            cmd.append("--no-recursive")
        if self.hidden_var.get():
            cmd.append("--include-hidden")
        if self.collect_empty_var.get():
            cmd.append("--collect-empty-dirs")
        else:
            cmd.append("--no-collect-empty-dirs")
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run(self, dry_run: bool) -> None:
        helper = _helper_script()
        if not helper.is_file():
            messagebox.showerror("Missing script", f"Could not find:\n{helper}")
            return

        raw = self.path_var.get().strip()
        if not raw:
            messagebox.showwarning("Folder", "Choose a folder first.")
            return

        base = Path(raw).expanduser()
        if not base.is_dir():
            messagebox.showerror("Folder", f"Not a directory:\n{base}")
            return

        cmd = self._build_cmd(dry_run)
        self._append_text("\n---\n$ " + " ".join(cmd) + "\n\n")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            self._append_text("Timed out after 1 hour.\n")
            return
        except OSError as e:
            self._append_text(f"Could not run helper: {e}\n")
            return

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if err:
            self._append_text(err + "\n")
        if out:
            try:
                data = json.loads(out)
                self._append_text(json.dumps(data, indent=2) + "\n")
            except json.JSONDecodeError:
                self._append_text(out + "\n")

        if proc.returncode != 0:
            self._append_text(f"\n(exit code {proc.returncode})\n")


def main() -> None:
    root = tk.Tk()
    TinkerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
