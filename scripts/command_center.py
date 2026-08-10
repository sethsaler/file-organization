#!/usr/bin/env python3
"""Native command-center UI for the file organizer.

The interface keeps the existing organizer and scheduler engines, but presents
their state through a safer preview-first workflow and a watch-folder overview.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from org_logging import append_history_entry, read_history
from org_manifest import list_manifests
from org_paths import normalize_folder_input
from org_rules import (
    FALLBACK_BUCKET,
    FALLBACK_NEEDS_REVIEW,
    FALLBACK_LEAVE,
    NEEDS_REVIEW_DIR_NAME,
    append_rule_for_review_choice,
    archive_mapping_template,
    load_rule_set,
    save_rule_set,
    starter_rule_set,
)
from org_safety import (
    SafetyItem,
    approve_review_item,
    handoff_to_dedupe,
    manifest_for_item,
    move_to_trash,
    original_source_for_review,
    restore_item_run,
    reveal_in_file_manager,
    scan_safety_items,
)
from org_watch_status import read_watch_status, watch_status_path
from schedule_config import SCHEDULE_MODE_WATCH, default_config_path, normalize_schedule_mode
from schedule_panel import SchedulePanel
from schedule_service import service_log_path


def _helper_script() -> Path:
    return _SCRIPT_DIR / "organize_by_filetype.py"


def _restore_script() -> Path:
    return _SCRIPT_DIR / "restore_from_backup.py"


def _rename_script() -> Path:
    return _SCRIPT_DIR / "rename_files_randomly.py"


def _display_time(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Never"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().strftime("%b %-d, %-I:%M %p")
    except (ValueError, OSError):
        return raw[:19].replace("T", " ")


def _short_path(path: str, limit: int = 64) -> str:
    return path if len(path) <= limit else "…" + path[-(limit - 1) :]


def _human_size(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024.0 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TB"


class CollapsibleFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc, title: str, *, expanded: bool = False) -> None:
        super().__init__(parent)
        self._title = title
        self._expanded = expanded
        self._button = ttk.Button(self, command=self._toggle)
        self._button.pack(fill="x")
        self.content = ttk.Frame(self, padding=(12, 10))
        self._sync()

    def _sync(self) -> None:
        marker = "Hide" if self._expanded else "Show"
        self._button.configure(text=f"{marker} {self._title}")
        if self._expanded:
            self.content.pack(fill="x", expand=True)
        else:
            self.content.pack_forget()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._sync()


class CompactSchedulePanel(SchedulePanel):
    """A compact command-center presentation over the existing scheduler engine."""

    def __init__(self, parent: tk.Misc, root: tk.Tk) -> None:
        super().__init__(parent, root, embedded=True)

        # Keep the proven scheduler logic and variables, but replace its dense
        # layout with a focused folder list and on-demand settings dialogs.
        for child in self.winfo_children():
            try:
                child.grid_remove()
            except tk.TclError:
                try:
                    child.pack_forget()
                except tk.TclError:
                    pass

        compact = ttk.Frame(self)
        compact.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        compact.columnconfigure(0, weight=1)
        compact.rowconfigure(4, weight=1)

        ttk.Label(compact, text="Watched folders", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            compact,
            text="Choose which folders run automatically and review their latest result.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 14))

        status = ttk.Frame(compact)
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.compact_status_var = tk.StringVar()
        ttk.Label(status, textvariable=self.compact_status_var, wraplength=650, justify="left").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(
            status,
            text="Enable automatic runs",
            variable=self.scheduler_var,
            command=self._on_scheduler_toggle,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.naming_notice_var = tk.StringVar()
        ttk.Label(status, textvariable=self.naming_notice_var, style="Warning.TLabel").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )

        ttk.Separator(compact).grid(row=3, column=0, sticky="ew", pady=14)
        list_frame = ttk.Frame(compact)
        list_frame.grid(row=4, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            list_frame,
            columns=("folder", "path", "status", "last_run", "threshold"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        for key, label in (
            ("folder", "Folder"),
            ("path", "Location"),
            ("status", "Status"),
            ("last_run", "Last result"),
            ("threshold", "Threshold"),
        ):
            self.tree.heading(key, text=label)
        self.tree.column("folder", width=115, stretch=False)
        self.tree.column("path", width=220)
        self.tree.column("status", width=90, stretch=False)
        self.tree.column("last_run", width=130, stretch=False)
        self.tree.column("threshold", width=82, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview).grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        actions = ttk.Frame(compact)
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="Add folder…", command=self._add_folder).pack(side="left")
        ttk.Button(actions, text="Edit selected…", command=self._show_folder_options).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Remove", command=self._remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Run selected now", command=self._run_selected_now).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Run all enabled", command=self._run_all_enabled_now).pack(side="left", padx=(8, 0))

        footer = ttk.Frame(compact)
        footer.grid(row=6, column=0, sticky="ew", pady=(18, 0))
        ttk.Label(
            footer,
            text="Folder changes save automatically. Existing files are never overwritten.",
            style="Success.TLabel",
        ).pack(side="left")
        ttk.Button(footer, text="Activity log…", command=self._show_activity_log).pack(side="right")
        ttk.Button(footer, text="Scheduler settings…", command=self._show_scheduler_settings).pack(
            side="right", padx=(0, 8)
        )

        self._tree_item_to_index = {}
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        super()._refresh_tree()
        if hasattr(self, "compact_status_var"):
            concise_status = self.next_run_var.get().split(" · Background scheduler:", 1)[0]
            self.compact_status_var.set(concise_status)
        if hasattr(self, "naming_notice_var"):
            count = sum(bool(job.random_names_after_organize) for job in self.cfg.folders)
            self.naming_notice_var.set(
                f"Random filenames are enabled for {count} folder{'s' if count != 1 else ''}."
                if count
                else ""
            )

    def _row_values(self, job: Any) -> tuple:
        path = Path(job.path).expanduser()
        mode = normalize_schedule_mode(self.cfg.schedule_mode)
        if not job.dry_run_verified:
            status = "Needs preview"
        elif not job.enabled:
            status = "Paused"
        elif self.cfg.scheduler_enabled and mode == SCHEDULE_MODE_WATCH:
            status = "Watching"
        else:
            status = "Scheduled"
        last = "Failed" if job.last_error else _display_time(job.last_run)
        threshold = "Always" if job.min_unsorted_threshold <= 0 else f"{job.min_unsorted_threshold} files"
        return (path.name or str(path), _short_path(str(path), 52), status, last, threshold)

    def _show_folder_options(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("Folder settings", "Select a folder first.")
            return
        self._on_tree_select()
        win = tk.Toplevel(self.root)
        win.title("Folder settings")
        win.geometry("700x720")
        win.minsize(620, 640)
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text="Folder settings", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="Changes save automatically for the selected folder.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 14))

        options = ttk.Frame(body)
        options.grid(row=2, column=0, sticky="nsew")
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)

        def check(parent: ttk.Frame, text: str, variable: tk.Variable, row: int) -> ttk.Checkbutton:
            widget = ttk.Checkbutton(parent, text=text, variable=variable, command=self._push_detail_and_save)
            widget.grid(row=row, column=0, sticky="w", pady=4)
            return widget

        left = ttk.LabelFrame(options, text="Organization", padding=12)
        right = ttk.LabelFrame(options, text="File handling", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        check(left, "Include in automatic runs", self.enabled_var, 0)
        check(left, "Include subfolders", self.recursive_var, 1)
        check(left, "Include hidden files", self.hidden_var, 2)
        check(left, "Collect empty folders", self.collect_empty_var, 3)
        check(left, "Organize immediate subfolders separately", self.expand_subfolders_var, 4)
        check(right, "Randomize filenames", self.random_names_after_organize_var, 0)
        check(right, "Skip existing random names", self.skip_randomly_renamed_var, 1)
        check(right, "Detect identical duplicates", self.detect_duplicates_var, 2)
        hardlink = check(right, "Use space-saving hardlinks", self.duplicates_hardlink_var, 3)
        check(right, "Sort into Year/Month folders", self.date_buckets_var, 4)
        hardlink.configure(state="normal" if self.detect_duplicates_var.get() else "disabled")

        strategy = ttk.LabelFrame(body, text="Recursive strategy", padding=10)
        strategy.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        ttk.Radiobutton(strategy, text="Flatten to root buckets", variable=self.strategy_var, value="flatten-root", command=self._push_detail_and_save).pack(side="left")
        ttk.Radiobutton(strategy, text="Organize in place", variable=self.strategy_var, value="in-place", command=self._push_detail_and_save).pack(side="left", padx=(16, 0))

        threshold = ttk.Frame(body)
        threshold.grid(row=4, column=0, sticky="w", pady=(14, 0))
        ttk.Label(threshold, text="Minimum unsorted files before running").pack(side="left")
        spin = tk.Spinbox(threshold, from_=0, to=999999, width=8, textvariable=self.min_unsorted_threshold_var, command=self._push_detail_and_save)
        spin.pack(side="left", padx=(8, 0))
        ttk.Label(threshold, text="0 means always run", style="Muted.TLabel").pack(side="left", padx=(8, 0))
        spin.bind("<FocusOut>", lambda _event: self._push_detail_and_save())
        spin.bind("<Return>", lambda _event: self._push_detail_and_save())

        routing = ttk.LabelFrame(body, text="Rules and archive routing", padding=10)
        routing.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        routing.columnconfigure(1, weight=1)
        ttk.Label(routing, text="Rules file").grid(row=0, column=0, sticky="w")
        rules_entry = ttk.Entry(routing, textvariable=self.rules_file_var)
        rules_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(
            routing,
            text="Choose…",
            command=lambda: self._choose_detail_file(self.rules_file_var, "Choose routing rules"),
        ).grid(row=0, column=2)
        ttk.Label(routing, text="If unmatched").grid(row=1, column=0, sticky="w", pady=(8, 0))
        unmatched = ttk.Combobox(
            routing,
            textvariable=self.unmatched_mode_var,
            values=(FALLBACK_BUCKET, FALLBACK_NEEDS_REVIEW, FALLBACK_LEAVE),
            state="readonly",
            width=16,
        )
        unmatched.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        unmatched.bind("<<ComboboxSelected>>", lambda _event: self._push_detail_and_save())
        ttk.Label(routing, text="Archive root").grid(row=2, column=0, sticky="w", pady=(8, 0))
        archive_entry = ttk.Entry(routing, textvariable=self.archive_root_var)
        archive_entry.grid(row=2, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))
        ttk.Button(routing, text="Choose…", command=lambda: self._choose_detail_folder(self.archive_root_var)).grid(row=2, column=2, pady=(8, 0))
        ttk.Label(routing, text="Folder mapping").grid(row=3, column=0, sticky="w", pady=(8, 0))
        mapping_entry = ttk.Entry(routing, textvariable=self.archive_mapping_var)
        mapping_entry.grid(row=3, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))
        ttk.Button(
            routing,
            text="Choose…",
            command=lambda: self._choose_detail_file(self.archive_mapping_var, "Choose archive mapping"),
        ).grid(row=3, column=2, pady=(8, 0))
        for entry in (rules_entry, archive_entry, mapping_entry):
            entry.bind("<FocusOut>", lambda _event: self._push_detail_and_save())
            entry.bind("<Return>", lambda _event: self._push_detail_and_save())

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, sticky="ew", pady=(22, 0))
        ttk.Button(buttons, text="Done", command=lambda: (self._push_detail_and_save(), win.destroy())).pack(side="right")

    def _push_detail_and_save(self) -> None:
        self._push_detail_to_job()
        self._save_quiet()

    def _choose_detail_file(self, variable: tk.StringVar, title: str) -> None:
        selected = filedialog.askopenfilename(title=title, filetypes=(("JSON", "*.json"),))
        if selected:
            variable.set(selected)
            self._push_detail_and_save()

    def _choose_detail_folder(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(title="Choose Archive root")
        if selected:
            variable.set(selected)
            self._push_detail_and_save()

    def _show_scheduler_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Scheduler settings")
        win.geometry("620x480")
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Scheduler settings", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="Technical timing controls live here so the folder list stays focused.", style="Muted.TLabel").pack(anchor="w", pady=(4, 16))

        timing = ttk.LabelFrame(body, text="Run mode", padding=12)
        timing.pack(fill="x")
        interval = ttk.Frame(timing)
        interval.pack(fill="x")
        ttk.Radiobutton(interval, text="Run every", variable=self.schedule_mode_var, value="interval").pack(side="left")
        tk.Spinbox(interval, from_=1, to=10080, width=8, textvariable=self.interval_var).pack(side="left", padx=(8, 0))
        ttk.Label(interval, text="minutes").pack(side="left", padx=(6, 0))
        daily = ttk.Frame(timing)
        daily.pack(fill="x", pady=(8, 0))
        ttk.Radiobutton(daily, text="Once daily at", variable=self.schedule_mode_var, value="daily").pack(side="left")
        ttk.Entry(daily, width=8, textvariable=self.daily_time_var).pack(side="left", padx=(8, 0))
        ttk.Label(daily, text="local time, 24-hour clock").pack(side="left", padx=(6, 0))
        ttk.Radiobutton(timing, text="Watch folders and run after files change", variable=self.schedule_mode_var, value="watch").pack(anchor="w", pady=(8, 0))

        advanced = ttk.LabelFrame(body, text="Advanced watch tuning", padding=12)
        advanced.pack(fill="x", pady=(14, 0))
        row = ttk.Frame(advanced)
        row.pack(fill="x")
        ttk.Label(row, text="Poll every").pack(side="left")
        tk.Spinbox(row, from_=0.05, to=60.0, increment=0.25, width=7, textvariable=self.watch_poll_var).pack(side="left", padx=(6, 0))
        ttk.Label(row, text="seconds; wait for quiet").pack(side="left", padx=(6, 0))
        tk.Spinbox(row, from_=0.0, to=60.0, increment=0.25, width=7, textvariable=self.watch_quiet_var).pack(side="left", padx=(6, 0))
        ttk.Label(row, text="seconds").pack(side="left", padx=(6, 0))
        parallel = ttk.Frame(advanced)
        parallel.pack(fill="x", pady=(8, 0))
        ttk.Label(parallel, text="Maximum parallel runs").pack(side="left")
        tk.Spinbox(parallel, from_=0, to=128, width=7, textvariable=self.max_parallel_var).pack(side="left", padx=(8, 0))
        ttk.Label(parallel, text="0 uses all enabled folders, capped at 32", style="Muted.TLabel").pack(side="left", padx=(8, 0))
        ttk.Checkbutton(advanced, text="Show macOS notifications after automatic runs", variable=self.notify_on_run_var).pack(anchor="w", pady=(8, 0))

        ttk.Label(body, text=f"Configuration: {self.config_path}", style="Muted.TLabel").pack(anchor="w", pady=(14, 0))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(18, 0))
        ttk.Button(buttons, text="Save", command=lambda: (self._save(), win.destroy())).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))

    def _show_activity_log(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Scheduler activity")
        win.geometry("780x430")
        text = scrolledtext.ScrolledText(win, wrap="word", font=("Menlo", 10))
        text.pack(fill="both", expand=True, padx=12, pady=12)
        try:
            content = self.out.get("1.0", "end").strip()
        except tk.TclError:
            content = ""
        text.insert("end", content or "No scheduler activity in this app session.")
        text.configure(state="disabled")


class CommandCenterApp:
    """Main native desktop application."""

    PRESETS = (
        "Standard organization",
        "Extended categories",
        "Downloader inbox",
        "Custom",
    )

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("File Organizer")
        self.root.geometry("1120x760")
        self.root.minsize(940, 650)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._configure_styles()
        self._out_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._active_proc: subprocess.Popen | None = None
        self._cancel_requested = False
        self._active_target = ""
        self._last_preview_fingerprint: tuple | None = None
        self._preview_change_count = 0
        self._last_manifest: str | None = None
        self._history_records: list[dict] = []

        # One-time organization state.
        self.path_var = tk.StringVar()
        self.preset_var = tk.StringVar(value=self.PRESETS[0])
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
        self.rules_file_var = tk.StringVar()
        self.unmatched_mode_var = tk.StringVar(value=FALLBACK_BUCKET)
        self.archive_root_var = tk.StringVar()
        self.archive_mapping_var = tk.StringVar()

        # Rules/review and Safety Center state.
        self.review_root_var = tk.StringVar()
        self.review_destination_var = tk.StringVar(value="Other/Reviewed")
        self.review_remember_var = tk.BooleanVar(value=True)
        self.review_criterion_var = tk.StringVar(value="Extension")
        self.review_rules_file_var = tk.StringVar(
            value=str(default_config_path().parent / "rules.json")
        )
        self._review_items: list[Path] = []
        self.safety_root_var = tk.StringVar()
        self.safety_status_var = tk.StringVar(value="")
        self._safety_items: list[SafetyItem] = []

        # Standalone random rename state.
        self.rename_path_var = tk.StringVar()
        self.rename_recursive_var = tk.BooleanVar(value=True)
        self.rename_hidden_var = tk.BooleanVar(value=True)
        self.rename_verbose_var = tk.BooleanVar(value=False)
        self.rename_skip_randomly_renamed_var = tk.BooleanVar(value=True)

        shell = ttk.Frame(root)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)

        self.sidebar = tk.Frame(shell, width=220, background="#f3f4f6")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        ttk.Separator(shell, orient="vertical").grid(row=0, column=0, sticky="nse", padx=(0, 0))

        self.main = ttk.Frame(shell)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(0, weight=1)

        self._pages: dict[str, ttk.Frame] = {}
        for key in ("overview", "organize", "rules", "watched", "history", "safety", "advanced"):
            page = ttk.Frame(self.main, padding=(28, 24))
            page.grid(row=0, column=0, sticky="nsew")
            self._pages[key] = page

        # SchedulePanel owns the live config and service actions used by Overview.
        self._build_watched_page(self._pages["watched"])
        self._build_overview_page(self._pages["overview"])
        self._build_organize_page(self._pages["organize"])
        self._build_rules_page(self._pages["rules"])
        self._build_history_page(self._pages["history"])
        self._build_safety_page(self._pages["safety"])
        self._build_advanced_page(self._pages["advanced"])
        self._build_sidebar()
        self._build_menus()
        self._bind_shortcuts()
        self._bind_preview_invalidation()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_page("overview")
        self._refresh_overview()

    # ------------------------------------------------------------------
    # Shell and navigation
    # ------------------------------------------------------------------

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("PageTitle.TLabel", font=("SF Pro Display", 24, "bold"))
        style.configure("SectionTitle.TLabel", font=("SF Pro Text", 15, "bold"))
        style.configure("StatusTitle.TLabel", font=("SF Pro Display", 21, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6368")
        style.configure("Success.TLabel", foreground="#14843c")
        style.configure("Warning.TLabel", foreground="#9a5a00")
        style.configure("Primary.TButton", font=("SF Pro Text", 13, "bold"), padding=(14, 8))
        style.configure("TButton", padding=(10, 6))
        style.configure("Treeview", rowheight=30, font=("SF Pro Text", 12))
        style.configure("Treeview.Heading", font=("SF Pro Text", 11, "bold"))

    def _build_sidebar(self) -> None:
        tk.Label(
            self.sidebar,
            text="File Organizer",
            background="#f3f4f6",
            foreground="#171717",
            font=("SF Pro Display", 19, "bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(24, 18))

        self._nav_buttons: dict[str, tk.Button] = {}
        labels = (
            ("overview", "Overview"),
            ("organize", "Organize once"),
            ("rules", "Rules & review"),
            ("watched", "Watched folders"),
            ("history", "History"),
            ("safety", "Safety center"),
            ("advanced", "Advanced settings"),
        )
        for key, label in labels:
            button = tk.Button(
                self.sidebar,
                text=label,
                command=lambda k=key: self._show_page(k),
                anchor="w",
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=18,
                pady=11,
                font=("SF Pro Text", 14),
                background="#f3f4f6",
                activebackground="#e5e9f2",
            )
            button.pack(fill="x", padx=10, pady=2)
            self._nav_buttons[key] = button

        safety = tk.Frame(self.sidebar, background="#f3f4f6")
        safety.pack(side="bottom", fill="x", padx=20, pady=22)
        tk.Label(
            safety,
            text="Safety & recovery",
            background="#f3f4f6",
            foreground="#171717",
            font=("SF Pro Text", 13, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            safety,
            text="Existing files are never overwritten.\nUndo is available from run history.",
            background="#f3f4f6",
            foreground="#60646c",
            justify="left",
            anchor="w",
            font=("SF Pro Text", 11),
        ).pack(fill="x", pady=(6, 0))

    def _build_menus(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Organize a Folder…", accelerator="⌘O", command=self._focus_organize)
        file_menu.add_command(label="Add Watched Folder…", command=self._add_watched_from_overview)
        file_menu.add_separator()
        file_menu.add_command(label="Close", accelerator="⌘W", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        for index, (key, label) in enumerate(
            (
                ("overview", "Overview"),
                ("organize", "Organize Once"),
                ("rules", "Rules & Review"),
                ("watched", "Watched Folders"),
                ("history", "History"),
                ("safety", "Safety Center"),
                ("advanced", "Advanced Settings"),
            ),
            start=1,
        ):
            view_menu.add_command(
                label=label,
                accelerator=f"⌘{index}",
                command=lambda page=key: self._show_page(page),
            )
        menubar.add_cascade(label="View", menu=view_menu)
        self.root.configure(menu=menubar)

    def _bind_shortcuts(self) -> None:
        for index, key in enumerate(("overview", "organize", "rules", "watched", "history", "safety", "advanced"), start=1):
            self.root.bind_all(f"<Command-Key-{index}>", lambda _event, page=key: self._show_page(page))
        self.root.bind_all("<Command-Key-o>", lambda _event: self._focus_organize())
        self.root.bind_all("<Command-Key-p>", lambda _event: self._run_organize(True))
        self.root.bind_all("<Command-Key-w>", lambda _event: self._on_close())

    def _focus_organize(self) -> None:
        self._show_page("organize")
        self.path_entry.focus_set()

    def _show_page(self, key: str) -> None:
        page = self._pages[key]
        self._current_page = key
        page.tkraise()
        for name, button in self._nav_buttons.items():
            selected = name == key
            button.configure(
                background="#dce8ff" if selected else "#f3f4f6",
                foreground="#075cc8" if selected else "#202124",
                font=("SF Pro Text", 14, "bold" if selected else "normal"),
            )
        if key == "history":
            self._refresh_history()
        elif key == "rules":
            self._sync_review_root_from_context()
            self._refresh_review_queue()
        elif key == "safety":
            self._sync_safety_root_from_context()
            self._refresh_safety_center()
        elif key == "overview":
            self._refresh_overview_once()

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------

    def _build_overview_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(5, weight=1)

        header = ttk.Frame(page)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.overview_status_title = tk.StringVar()
        self.overview_status_subtitle = tk.StringVar()
        self.overview_health_var = tk.StringVar()
        self.overview_warning_var = tk.StringVar()
        self.pause_button_var = tk.StringVar()
        ttk.Label(header, textvariable=self.overview_status_title, style="StatusTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.overview_status_subtitle, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.overview_warning_var, style="Warning.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(header, textvariable=self.overview_health_var, style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Button(header, textvariable=self.pause_button_var, command=self._toggle_watching).grid(row=0, column=1, rowspan=4, sticky="e")

        ttk.Separator(page).grid(row=1, column=0, sticky="ew", pady=20)
        ttk.Label(page, text="Watched folders", style="SectionTitle.TLabel").grid(row=2, column=0, sticky="w")

        self.overview_tree = ttk.Treeview(
            page,
            columns=("folder", "path", "status", "last", "threshold"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        for key, text in (("folder", "Folder"), ("path", "Location"), ("status", "Status"), ("last", "Last result"), ("threshold", "Run at")):
            self.overview_tree.heading(key, text=text)
        self.overview_tree.column("folder", width=125, anchor="w", stretch=False)
        self.overview_tree.column("path", width=245, anchor="w")
        self.overview_tree.column("status", width=90, anchor="w", stretch=False)
        self.overview_tree.column("last", width=130, anchor="w", stretch=False)
        self.overview_tree.column("threshold", width=82, anchor="center", stretch=False)
        self.overview_tree.grid(row=3, column=0, sticky="nsew", pady=(10, 8))

        watched_actions = ttk.Frame(page)
        watched_actions.grid(row=4, column=0, sticky="ew")
        ttk.Button(watched_actions, text="Add watched folder…", command=self._add_watched_from_overview).pack(side="left")
        ttk.Button(watched_actions, text="View watched folders", command=lambda: self._show_page("watched")).pack(side="right")

        lower = ttk.Frame(page)
        lower.grid(row=5, column=0, sticky="nsew", pady=(22, 0))
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(1, weight=1)
        ttk.Label(lower, text="Recent activity", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.recent_tree = ttk.Treeview(
            lower,
            columns=("time", "folder", "result", "summary"),
            show="headings",
            height=4,
        )
        for key, text in (("time", "Time"), ("folder", "Folder"), ("result", "Result"), ("summary", "Summary")):
            self.recent_tree.heading(key, text=text)
        self.recent_tree.column("time", width=150)
        self.recent_tree.column("folder", width=160)
        self.recent_tree.column("result", width=100)
        self.recent_tree.column("summary", width=360)
        self.recent_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 8))
        activity_actions = ttk.Frame(lower)
        activity_actions.grid(row=2, column=0, sticky="ew")
        ttk.Button(activity_actions, text="View full history…", command=lambda: self._show_page("history")).pack(side="left")
        self.undo_recent_button = ttk.Button(activity_actions, text="Undo most recent run", command=self._undo_most_recent)
        self.undo_recent_button.pack(side="left", padx=(8, 0))
        ttk.Button(activity_actions, text="Organize a folder…", style="Primary.TButton", command=lambda: self._show_page("organize")).pack(side="right")

    def _refresh_overview(self) -> None:
        self._refresh_overview_once()
        try:
            if self.root.winfo_exists():
                self.root.after(1500, self._refresh_overview)
        except tk.TclError:
            pass

    def _refresh_overview_once(self) -> None:
        cfg = self.schedule_panel.cfg
        enabled = [job for job in cfg.folders if job.enabled]
        watching = bool(cfg.scheduler_enabled) and normalize_schedule_mode(cfg.schedule_mode) == SCHEDULE_MODE_WATCH
        watch_health = read_watch_status()
        folder_health = watch_health.get("folders") if isinstance(watch_health.get("folders"), dict) else {}
        if watching:
            self.overview_status_title.set(f"Organizer is watching {len(enabled)} folder{'s' if len(enabled) != 1 else ''}")
            self.overview_status_subtitle.set("Files will be organized automatically when they arrive.")
            self.pause_button_var.set("Pause all watching")
        elif cfg.scheduler_enabled:
            self.overview_status_title.set(f"Automatic organization is active for {len(enabled)} folder{'s' if len(enabled) != 1 else ''}")
            self.overview_status_subtitle.set("Open Watched folders to review the current schedule.")
            self.pause_button_var.set("Pause automatic runs")
        else:
            self.overview_status_title.set("Automatic organization is paused")
            self.overview_status_subtitle.set("One-time organization and previews are still available.")
            self.pause_button_var.set("Resume automatic runs")
        if watching and watch_health:
            backend = str(watch_health.get("backend") or "unknown backend")
            pending = int(watch_health.get("pending_count", 0) or 0)
            running = int(watch_health.get("running_count", 0) or 0)
            self.overview_health_var.set(
                f"Watcher: {backend} · {running} running · {pending} waiting · health updated {_display_time(watch_health.get('updated_at'))}"
            )
        elif watching:
            self.overview_health_var.set(f"Waiting for watcher health at {watch_status_path()}")
        else:
            self.overview_health_var.set("")
        random_name_count = sum(bool(job.random_names_after_organize) for job in cfg.folders)
        self.overview_warning_var.set(
            f"Random filenames are enabled for {random_name_count} watched folder{'s' if random_name_count != 1 else ''}."
            if random_name_count
            else ""
        )

        for item in self.overview_tree.get_children():
            self.overview_tree.delete(item)
        for job in cfg.folders:
            path = Path(job.path).expanduser()
            if not job.dry_run_verified:
                status = "Needs preview"
            elif not job.enabled:
                status = "Paused"
            elif watching:
                detail = folder_health.get(job.path) if isinstance(folder_health, dict) else None
                state = str((detail or {}).get("state") or "idle") if isinstance(detail, dict) else "idle"
                status = {
                    "running": "Running",
                    "dirty": "Waiting",
                    "waiting": "Waiting",
                    "error": "Failed",
                }.get(state, "Watching")
            else:
                status = "Scheduled"
            if job.last_error:
                last = "Failed"
            else:
                last = _display_time(job.last_run)
            threshold = "Always" if job.min_unsorted_threshold <= 0 else f"{job.min_unsorted_threshold} files"
            self.overview_tree.insert(
                "",
                "end",
                values=(path.name or str(path), _short_path(str(path), 52), status, last, threshold),
            )

        records = read_history(5)
        for item in self.recent_tree.get_children():
            self.recent_tree.delete(item)
        for rec in records:
            summary = self._history_summary(rec)
            path = Path(str(rec.get("path", ""))).expanduser()
            self.recent_tree.insert(
                "",
                "end",
                values=(_display_time(rec.get("ts")), path.name or "—", "Success" if rec.get("ok") else "Failed", summary),
            )
        can_undo = bool(records and records[0].get("backup_manifest") and Path(str(records[0]["backup_manifest"])).is_file())
        self.undo_recent_button.configure(state="normal" if can_undo else "disabled")

    def _toggle_watching(self) -> None:
        self.schedule_panel.scheduler_var.set(not bool(self.schedule_panel.scheduler_var.get()))
        self.schedule_panel._on_scheduler_toggle()
        self._refresh_overview_once()

    def _add_watched_from_overview(self) -> None:
        self._show_page("watched")
        self.schedule_panel._add_folder()

    # ------------------------------------------------------------------
    # One-time organize
    # ------------------------------------------------------------------

    def _build_organize_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(6, weight=1)
        ttk.Label(page, text="Organize a folder", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            page,
            text="Choose a folder, preview the outcome, then organize with a recoverable backup.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 18))

        folder_row = ttk.Frame(page)
        folder_row.grid(row=2, column=0, sticky="ew")
        folder_row.columnconfigure(1, weight=1)
        ttk.Label(folder_row, text="Folder", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.path_entry = ttk.Entry(folder_row, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(folder_row, text="Browse…", command=lambda: self._browse(self.path_var, "Choose folder to organize")).grid(row=0, column=2, padx=(8, 0))

        preset_row = ttk.Frame(page)
        preset_row.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(preset_row, text="Preset").pack(side="left")
        preset = ttk.Combobox(preset_row, state="readonly", textvariable=self.preset_var, values=self.PRESETS, width=26)
        preset.pack(side="left", padx=(10, 0))
        preset.bind("<<ComboboxSelected>>", self._on_preset_change)
        ttk.Label(preset_row, text="Existing destination files are never overwritten.", style="Success.TLabel").pack(side="right")

        advanced = CollapsibleFrame(page, "advanced organization settings")
        advanced.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self._build_organize_advanced(advanced.content)

        preview = ttk.LabelFrame(page, text="Preview", padding=14)
        preview.grid(row=5, column=0, sticky="ew", pady=(16, 0))
        preview.columnconfigure(0, weight=1)
        self.preview_title_var = tk.StringVar(value="Choose a folder to preview changes")
        self.preview_subtitle_var = tk.StringVar(value="No files have been changed.")
        self.preview_counts_var = tk.StringVar(value="")
        self.preview_warning_var = tk.StringVar(value="")
        ttk.Label(preview, textvariable=self.preview_title_var, style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(preview, textvariable=self.preview_subtitle_var, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Label(preview, textvariable=self.preview_counts_var).grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Label(preview, textvariable=self.preview_warning_var, style="Warning.TLabel").grid(row=3, column=0, sticky="w", pady=(6, 0))

        move_frame = ttk.Frame(page)
        move_frame.grid(row=6, column=0, sticky="nsew", pady=(12, 0))
        move_frame.columnconfigure(0, weight=1)
        move_frame.rowconfigure(0, weight=1)
        self.planned_tree = ttk.Treeview(move_frame, columns=("source", "destination", "reason"), show="headings", height=8)
        self.planned_tree.heading("source", text="Current location")
        self.planned_tree.heading("destination", text="Destination")
        self.planned_tree.heading("reason", text="Why")
        self.planned_tree.column("source", width=220)
        self.planned_tree.column("destination", width=280)
        self.planned_tree.column("reason", width=240)
        self.planned_tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(move_frame, orient="vertical", command=self.planned_tree.yview).grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(page)
        actions.grid(row=7, column=0, sticky="ew", pady=(14, 0))
        self.preview_button = ttk.Button(actions, text="Preview changes", style="Primary.TButton", command=lambda: self._run_organize(True))
        self.preview_button.pack(side="left")
        self.organize_button = ttk.Button(actions, text="Organize files", style="Primary.TButton", command=lambda: self._run_organize(False), state="disabled")
        self.organize_button.pack(side="left", padx=(8, 0))
        self.watch_folder_button = ttk.Button(actions, text="Watch this folder…", command=self._add_to_schedule)
        self.watch_folder_button.pack(side="left", padx=(8, 0))
        self.restore_button = ttk.Button(actions, text="Restore…", command=self._restore_latest)
        self.restore_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="Stop", command=self._cancel, state="disabled")

    def _build_organize_advanced(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        left = ttk.Frame(parent)
        right = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 20))
        right.grid(row=0, column=1, sticky="nw")
        ttk.Checkbutton(left, text="Include subfolders", variable=self.recursive_var).pack(anchor="w")
        ttk.Checkbutton(left, text="Include hidden files and folders", variable=self.hidden_var).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(left, text="Collect empty folders into For Deletion", variable=self.collect_empty_var).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(left, text="Exclude project and dependency folders", variable=self.exclude_defaults_var).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(left, text="Organize each immediate subfolder separately", variable=self.expand_subfolders_var).pack(anchor="w", pady=(4, 0))

        ttk.Checkbutton(right, text="Detect identical duplicates", variable=self.detect_duplicates_var).pack(anchor="w")
        self.hardlink_check = ttk.Checkbutton(right, text="Keep duplicates as space-saving hardlinks", variable=self.duplicates_hardlink_var)
        self.hardlink_check.pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(right, text="Sort into Year/Month folders", variable=self.date_buckets_var).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(right, text="Randomize filenames after organizing", variable=self.rename_after_organize_var).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(right, text="Identify extensionless files by content", variable=self.mime_var).pack(anchor="w", pady=(4, 0))

        choices = ttk.Frame(parent)
        choices.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(choices, text="Profile").pack(side="left")
        ttk.Combobox(choices, textvariable=self.profile_var, values=("standard", "extended"), state="readonly", width=12).pack(side="left", padx=(6, 18))
        ttk.Label(choices, text="Recursive strategy").pack(side="left")
        ttk.Radiobutton(choices, text="Flatten to root buckets", variable=self.strategy_var, value="flatten-root").pack(side="left", padx=(6, 4))
        ttk.Radiobutton(choices, text="In place", variable=self.strategy_var, value="in-place").pack(side="left")

        routing = ttk.LabelFrame(parent, text="Rules and archive routing", padding=10)
        routing.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        routing.columnconfigure(1, weight=1)
        ttk.Label(routing, text="Rules file").grid(row=0, column=0, sticky="w")
        ttk.Entry(routing, textvariable=self.rules_file_var).grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(
            routing,
            text="Choose…",
            command=lambda: self._browse_file(self.rules_file_var, "Choose routing rules", (("JSON", "*.json"),)),
        ).grid(row=0, column=2)
        ttk.Label(routing, text="If unmatched").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            routing,
            textvariable=self.unmatched_mode_var,
            values=(FALLBACK_BUCKET, FALLBACK_NEEDS_REVIEW, FALLBACK_LEAVE),
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(routing, text="Archive root").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(routing, textvariable=self.archive_root_var).grid(row=2, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))
        ttk.Button(routing, text="Choose…", command=lambda: self._browse(self.archive_root_var, "Choose iCloud Archive root")).grid(row=2, column=2, pady=(8, 0))
        ttk.Label(routing, text="Folder mapping").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(routing, textvariable=self.archive_mapping_var).grid(row=3, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))
        ttk.Button(
            routing,
            text="Choose…",
            command=lambda: self._browse_file(self.archive_mapping_var, "Choose archive folder mapping", (("JSON", "*.json"),)),
        ).grid(row=3, column=2, pady=(8, 0))
        ttk.Label(
            routing,
            text="Use either a rules file or an Archive root. Archive mappings route known creator folders; unknown folders are held for review.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _bind_preview_invalidation(self) -> None:
        variables = (
            self.path_var,
            self.recursive_var,
            self.strategy_var,
            self.normalize_var,
            self.profile_var,
            self.hidden_var,
            self.collect_empty_var,
            self.exclude_defaults_var,
            self.mime_var,
            self.detect_duplicates_var,
            self.duplicates_hardlink_var,
            self.date_buckets_var,
            self.rename_after_organize_var,
            self.skip_randomly_renamed_var,
            self.expand_subfolders_var,
            self.rules_file_var,
            self.unmatched_mode_var,
            self.archive_root_var,
            self.archive_mapping_var,
        )
        for var in variables:
            var.trace_add("write", lambda *_args: self._invalidate_preview())
        self.detect_duplicates_var.trace_add("write", lambda *_args: self._sync_dependent_controls())
        self._sync_dependent_controls()

    def _sync_dependent_controls(self) -> None:
        try:
            self.hardlink_check.configure(state="normal" if self.detect_duplicates_var.get() else "disabled")
            if not self.detect_duplicates_var.get() and self.duplicates_hardlink_var.get():
                self.duplicates_hardlink_var.set(False)
        except tk.TclError:
            pass

    def _invalidate_preview(self) -> None:
        self._last_preview_fingerprint = None
        self._preview_change_count = 0
        if hasattr(self, "organize_button"):
            self.organize_button.configure(state="disabled", text="Organize files")
        if hasattr(self, "preview_title_var"):
            self.preview_title_var.set("Preview required")
            self.preview_subtitle_var.set("Settings changed. Preview again before organizing.")
            self.preview_counts_var.set("")
            self.preview_warning_var.set("")

    def _on_preset_change(self, _event: Any = None) -> None:
        preset = self.preset_var.get()
        if preset == "Custom":
            return
        self.recursive_var.set(True)
        self.strategy_var.set("flatten-root")
        self.normalize_var.set("standard")
        self.hidden_var.set(True)
        self.collect_empty_var.set(True)
        self.exclude_defaults_var.set(True)
        self.detect_duplicates_var.set(False)
        self.date_buckets_var.set(False)
        self.profile_var.set("extended" if preset == "Extended categories" else "standard")
        self.rename_after_organize_var.set(False)
        if preset == "Downloader inbox":
            self.rules_file_var.set("")
            self.unmatched_mode_var.set(FALLBACK_NEEDS_REVIEW)
        else:
            self.rules_file_var.set("")
            self.archive_root_var.set("")
            self.archive_mapping_var.set("")
            self.unmatched_mode_var.set(FALLBACK_BUCKET)

    def _preview_fingerprint(self) -> tuple:
        return (
            self.path_var.get().strip(),
            self.recursive_var.get(),
            self.strategy_var.get(),
            self.normalize_var.get(),
            self.profile_var.get(),
            self.hidden_var.get(),
            self.collect_empty_var.get(),
            self.exclude_defaults_var.get(),
            self.mime_var.get(),
            self.detect_duplicates_var.get(),
            self.duplicates_hardlink_var.get(),
            self.date_buckets_var.get(),
            self.rename_after_organize_var.get(),
            self.expand_subfolders_var.get(),
            self.rules_file_var.get().strip(),
            self.unmatched_mode_var.get(),
            self.archive_root_var.get().strip(),
            self.archive_mapping_var.get().strip(),
        )

    # ------------------------------------------------------------------
    # Rules and review queue
    # ------------------------------------------------------------------

    def _build_rules_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(6, weight=1)
        ttk.Label(page, text="Rules & review", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            page,
            text="Use deterministic rules for known files and hold everything else for a human decision.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 16))

        setup = ttk.LabelFrame(page, text="Rule setup", padding=12)
        setup.grid(row=2, column=0, sticky="ew")
        setup.columnconfigure(1, weight=1)
        ttk.Label(setup, text="Organizer root").grid(row=0, column=0, sticky="w")
        ttk.Entry(setup, textvariable=self.review_root_var).grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(setup, text="Choose…", command=lambda: self._browse(self.review_root_var, "Choose organizer root")).grid(row=0, column=2)
        ttk.Label(setup, text="Rules file").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(setup, textvariable=self.review_rules_file_var).grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))
        ttk.Button(
            setup,
            text="Choose…",
            command=lambda: self._browse_file(self.review_rules_file_var, "Choose routing rules", (("JSON", "*.json"),)),
        ).grid(row=1, column=2, pady=(8, 0))
        setup_actions = ttk.Frame(setup)
        setup_actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(setup_actions, text="Create starter rules", command=self._create_starter_rules).pack(side="left")
        ttk.Button(setup_actions, text="Validate rules", command=self._validate_rules).pack(side="left", padx=(8, 0))
        ttk.Button(setup_actions, text="Use for Organize once", command=self._use_rules_for_organize).pack(side="left", padx=(8, 0))

        archive_box = CollapsibleFrame(page, "Downloader → Archive recipe")
        archive_box.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        archive = archive_box.content
        archive.columnconfigure(1, weight=1)
        ttk.Label(archive, text="Archive root").grid(row=0, column=0, sticky="w")
        ttk.Entry(archive, textvariable=self.archive_root_var).grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(archive, text="Choose…", command=lambda: self._browse(self.archive_root_var, "Choose Archive root")).grid(row=0, column=2)
        ttk.Label(archive, text="Creator mapping").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(archive, textvariable=self.archive_mapping_var).grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=(8, 0))
        ttk.Button(archive, text="Create…", command=self._create_archive_mapping).grid(row=1, column=2, pady=(8, 0))
        ttk.Label(
            archive,
            text="Loose media goes to exact Recents category folders. Known creator folders use the mapping; unknown folders go to Needs Review.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(archive, text="Use archive recipe for Organize once", command=self._use_archive_for_organize).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        review_header = ttk.Frame(page)
        review_header.grid(row=4, column=0, sticky="ew", pady=(18, 8))
        ttk.Label(review_header, text="Needs Review", style="SectionTitle.TLabel").pack(side="left")
        self.review_status_var = tk.StringVar(value="")
        ttk.Label(review_header, textvariable=self.review_status_var, style="Muted.TLabel").pack(side="left", padx=(10, 0))
        ttk.Button(review_header, text="Refresh", command=self._refresh_review_queue).pack(side="right")

        decision = ttk.Frame(page)
        decision.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(decision, text="Destination").pack(side="left")
        ttk.Entry(decision, textvariable=self.review_destination_var, width=28).pack(side="left", padx=(6, 12))
        ttk.Checkbutton(decision, text="Remember as rule", variable=self.review_remember_var).pack(side="left")
        ttk.Combobox(
            decision,
            textvariable=self.review_criterion_var,
            values=("Extension", "Filename", "Parent folder"),
            state="readonly",
            width=14,
        ).pack(side="left", padx=(6, 0))

        frame = ttk.Frame(page)
        frame.grid(row=6, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.review_tree = ttk.Treeview(
            frame,
            columns=("name", "location", "size", "modified"),
            show="headings",
            selectmode="browse",
        )
        for key, label in (("name", "Item"), ("location", "Review location"), ("size", "Size"), ("modified", "Modified")):
            self.review_tree.heading(key, text=label)
        self.review_tree.column("name", width=155)
        self.review_tree.column("location", width=330)
        self.review_tree.column("size", width=75, anchor="e", stretch=False)
        self.review_tree.column("modified", width=125, stretch=False)
        self.review_tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(frame, orient="vertical", command=self.review_tree.yview).grid(row=0, column=1, sticky="ns")
        actions = ttk.Frame(page)
        actions.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="Approve selected", style="Primary.TButton", command=self._approve_review_selected).pack(side="left")
        ttk.Button(actions, text="Reveal in Finder", command=self._reveal_review_selected).pack(side="left", padx=(8, 0))

    def _sync_review_root_from_context(self) -> None:
        if self.review_root_var.get().strip():
            return
        current = self.path_var.get().strip()
        if current:
            self.review_root_var.set(current)
        elif self.schedule_panel.cfg.folders:
            self.review_root_var.set(self.schedule_panel.cfg.folders[0].path)

    def _create_starter_rules(self) -> None:
        raw = self.review_rules_file_var.get().strip()
        if not raw:
            raw = str(default_config_path().parent / "rules.json")
            self.review_rules_file_var.set(raw)
        path = Path(raw).expanduser()
        if path.exists() and not messagebox.askyesno("Replace rules", f"Replace the existing rules file?\n{path}"):
            return
        try:
            save_rule_set(path, starter_rule_set())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Rules", str(exc))
            return
        self.rules_file_var.set(str(path))
        self.unmatched_mode_var.set(FALLBACK_NEEDS_REVIEW)
        messagebox.showinfo("Rules", f"Starter rules created at:\n{path}")

    def _validate_rules(self) -> None:
        path = Path(self.review_rules_file_var.get().strip()).expanduser()
        try:
            rules = load_rule_set(path)
        except ValueError as exc:
            messagebox.showerror("Rules", str(exc))
            return
        enabled = sum(1 for rule in rules.rules if rule.enabled)
        messagebox.showinfo("Rules", f"Valid: {enabled} enabled rule{'s' if enabled != 1 else ''}; unmatched → {rules.unmatched}.")

    def _use_rules_for_organize(self) -> None:
        root = self.review_root_var.get().strip()
        rules = self.review_rules_file_var.get().strip()
        if not root or not rules:
            messagebox.showwarning("Rules", "Choose an organizer root and rules file first.")
            return
        self.path_var.set(root)
        self.rules_file_var.set(rules)
        self.archive_root_var.set("")
        self.archive_mapping_var.set("")
        try:
            self.unmatched_mode_var.set(load_rule_set(Path(rules)).unmatched)
        except ValueError:
            self.unmatched_mode_var.set(FALLBACK_NEEDS_REVIEW)
        self._show_page("organize")

    def _create_archive_mapping(self) -> None:
        initial = self.archive_mapping_var.get().strip() or str(default_config_path().parent / "archive-mapping.json")
        selected = filedialog.asksaveasfilename(
            title="Create archive folder mapping",
            initialfile=Path(initial).name,
            initialdir=str(Path(initial).expanduser().parent),
            defaultextension=".json",
            filetypes=(("JSON", "*.json"),),
        )
        if not selected:
            return
        path = Path(selected)
        if path.exists() and not messagebox.askyesno("Replace mapping", f"Replace the existing mapping?\n{path}"):
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(archive_mapping_template(), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Archive mapping", str(exc))
            return
        self.archive_mapping_var.set(str(path))
        messagebox.showinfo("Archive mapping", f"Mapping template created at:\n{path}")

    def _use_archive_for_organize(self) -> None:
        source = self.review_root_var.get().strip() or self.path_var.get().strip()
        archive = self.archive_root_var.get().strip()
        if not source or not archive:
            messagebox.showwarning("Archive recipe", "Choose the Downloader source and Archive root first.")
            return
        self.path_var.set(source)
        self.rules_file_var.set("")
        self.unmatched_mode_var.set(FALLBACK_NEEDS_REVIEW)
        self.preset_var.set("Downloader inbox")
        self.rename_after_organize_var.set(False)
        self._show_page("organize")

    def _refresh_review_queue(self) -> None:
        for item in self.review_tree.get_children():
            self.review_tree.delete(item)
        raw = self.review_root_var.get().strip()
        if not raw:
            self.review_status_var.set("Choose an organizer root")
            self._review_items = []
            return
        root = normalize_folder_input(raw)
        review_root = root / NEEDS_REVIEW_DIR_NAME
        if not review_root.is_dir():
            self.review_status_var.set("Queue is empty")
            self._review_items = []
            return
        try:
            items = [path for path in review_root.rglob("*") if path.is_file()]
        except OSError:
            items = []
        def modified_time(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        items.sort(key=modified_time, reverse=True)
        self._review_items = items[:1000]
        for index, path in enumerate(self._review_items):
            try:
                stat = path.stat()
                size = _human_size(stat.st_size)
                modified = _display_time(datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat())
            except OSError:
                size, modified = "—", "Unknown"
            self.review_tree.insert(
                "",
                "end",
                iid=f"review-{index}",
                values=(path.name, _short_path(str(path.relative_to(root)), 58), size, modified),
            )
        self.review_status_var.set(f"{len(self._review_items)} item{'s' if len(self._review_items) != 1 else ''}")

    def _selected_review_item(self) -> Optional[Path]:
        selection = self.review_tree.selection()
        if not selection:
            messagebox.showwarning("Needs Review", "Select an item first.")
            return None
        try:
            return self._review_items[int(selection[0].split("-")[-1])]
        except (ValueError, IndexError):
            return None

    def _approve_review_selected(self) -> None:
        item = self._selected_review_item()
        if item is None:
            return
        root = normalize_folder_input(self.review_root_var.get().strip())
        destination = self.review_destination_var.get().strip()
        if not messagebox.askyesno("Approve reviewed item", f"Move:\n{item.name}\n\nTo:\n{destination}\n\nA recovery backup will be created."):
            return
        original = original_source_for_review(root, item)
        try:
            target, _manifest = approve_review_item(root, item, destination)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Needs Review", str(exc))
            return
        rule_note = ""
        if self.review_remember_var.get():
            rules_path = Path(self.review_rules_file_var.get().strip()).expanduser()
            try:
                rule = append_rule_for_review_choice(
                    rules_path,
                    source=original,
                    destination=destination,
                    criterion=self.review_criterion_var.get(),
                )
                self.rules_file_var.set(str(rules_path))
                self.unmatched_mode_var.set(FALLBACK_NEEDS_REVIEW)
                rule_note = f"\n\nRemembered rule: {rule.name}"
            except (OSError, ValueError) as exc:
                rule_note = f"\n\nThe file moved, but the rule could not be saved: {exc}"
        messagebox.showinfo("Needs Review", f"Moved to:\n{target}{rule_note}")
        self._refresh_review_queue()

    def _reveal_review_selected(self) -> None:
        item = self._selected_review_item()
        if item is not None and not reveal_in_file_manager(item):
            messagebox.showerror("Finder", "The selected item could not be revealed.")

    # ------------------------------------------------------------------
    # Watched folders
    # ------------------------------------------------------------------

    def _build_watched_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        self.schedule_panel = CompactSchedulePanel(page, self.root)
        self.schedule_panel.grid(row=0, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    # History and recovery
    # ------------------------------------------------------------------

    def _build_history_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)
        ttk.Label(page, text="Run history", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(page, text="Review results and restore any run that still has a backup.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 18))
        self.history_status_var = tk.StringVar(value="")
        ttk.Label(page, textvariable=self.history_status_var).grid(row=2, column=0, sticky="w")
        frame = ttk.Frame(page)
        frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.history_tree = ttk.Treeview(frame, columns=("time", "folder", "result", "summary", "undo"), show="headings")
        for key, text in (("time", "Time"), ("folder", "Folder"), ("result", "Result"), ("summary", "Summary"), ("undo", "Recovery")):
            self.history_tree.heading(key, text=text)
        self.history_tree.column("time", width=130, stretch=False)
        self.history_tree.column("folder", width=140, stretch=False)
        self.history_tree.column("result", width=80, stretch=False)
        self.history_tree.column("summary", width=285)
        self.history_tree.column("undo", width=105, stretch=False)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(frame, orient="vertical", command=self.history_tree.yview).grid(row=0, column=1, sticky="ns")
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_select)
        actions = ttk.Frame(page)
        actions.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="Refresh", command=self._refresh_history).pack(side="left")
        self.undo_history_button = ttk.Button(actions, text="Undo selected run…", command=self._undo_selected_history, state="disabled")
        self.undo_history_button.pack(side="left", padx=(8, 0))

    def _refresh_history(self) -> None:
        self._history_records = read_history(200)
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for idx, rec in enumerate(self._history_records):
            path = Path(str(rec.get("path", ""))).expanduser()
            manifest = str(rec.get("backup_manifest") or "")
            undo = "Undo available" if manifest and Path(manifest).is_file() else "Not available"
            self.history_tree.insert(
                "",
                "end",
                iid=f"history-{idx}",
                values=(
                    _display_time(rec.get("ts")),
                    path.name or "—",
                    "Success" if rec.get("ok") else "Failed",
                    self._history_summary(rec),
                    undo,
                ),
            )
        self.history_status_var.set(f"{len(self._history_records)} recent run{'s' if len(self._history_records) != 1 else ''}")
        self.undo_history_button.configure(state="disabled")

    @staticmethod
    def _history_summary(rec: dict) -> str:
        if not rec.get("ok"):
            return str(rec.get("error") or "Run failed")
        moved = rec.get("files_moved")
        parts = [f"{moved} files organized" if moved is not None else "Completed"]
        categories = rec.get("moved_by_category")
        if isinstance(categories, dict):
            visible = [f"{name} {count}" for name, count in categories.items() if count]
            if visible:
                parts.append(", ".join(visible[:4]))
        collisions = rec.get("name_collisions_resolved")
        if collisions:
            parts.append(f"{collisions} collision{'s' if collisions != 1 else ''} resolved")
        needs_review = rec.get("needs_review_files")
        if needs_review:
            parts.append(f"{needs_review} held for review")
        external = rec.get("external_moves")
        if external:
            parts.append(f"{external} archived")
        return " · ".join(parts)

    def _on_history_select(self, _event: Any = None) -> None:
        selection = self.history_tree.selection()
        if not selection:
            self.undo_history_button.configure(state="disabled")
            return
        try:
            idx = int(selection[0].split("-")[-1])
            rec = self._history_records[idx]
        except (ValueError, IndexError):
            self.undo_history_button.configure(state="disabled")
            return
        manifest = str(rec.get("backup_manifest") or "")
        self.undo_history_button.configure(state="normal" if manifest and Path(manifest).is_file() else "disabled")

    def _undo_selected_history(self) -> None:
        selection = self.history_tree.selection()
        if not selection:
            return
        idx = int(selection[0].split("-")[-1])
        self._restore_manifest(str(self._history_records[idx].get("backup_manifest") or ""))

    def _undo_most_recent(self) -> None:
        records = read_history(1)
        if records:
            self._restore_manifest(str(records[0].get("backup_manifest") or ""))

    # ------------------------------------------------------------------
    # Safety Center
    # ------------------------------------------------------------------

    def _build_safety_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        ttk.Label(page, text="Safety center", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            page,
            text="Review held items, recover their originating run, hand media to Dedupe, or move selected items to the macOS Trash.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 16))

        chooser = ttk.Frame(page)
        chooser.grid(row=2, column=0, sticky="ew")
        chooser.columnconfigure(1, weight=1)
        ttk.Label(chooser, text="Organizer root").grid(row=0, column=0, sticky="w")
        ttk.Entry(chooser, textvariable=self.safety_root_var).grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(chooser, text="Choose…", command=lambda: self._browse(self.safety_root_var, "Choose organizer root")).grid(row=0, column=2)
        ttk.Button(chooser, text="Refresh", command=self._refresh_safety_center).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(chooser, textvariable=self.safety_status_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        ttk.Label(page, text="Held items", style="SectionTitle.TLabel").grid(row=3, column=0, sticky="w", pady=(18, 8))
        frame = ttk.Frame(page)
        frame.grid(row=4, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.safety_tree = ttk.Treeview(
            frame,
            columns=("kind", "item", "root", "files", "size", "modified", "recovery"),
            show="headings",
            selectmode="browse",
        )
        for key, label in (
            ("kind", "Area"),
            ("item", "Item"),
            ("root", "Organizer root"),
            ("files", "Files"),
            ("size", "Size"),
            ("modified", "Modified"),
            ("recovery", "Recovery"),
        ):
            self.safety_tree.heading(key, text=label)
        self.safety_tree.column("kind", width=90, stretch=False)
        self.safety_tree.column("item", width=140)
        self.safety_tree.column("root", width=170)
        self.safety_tree.column("files", width=45, anchor="e", stretch=False)
        self.safety_tree.column("size", width=70, anchor="e", stretch=False)
        self.safety_tree.column("modified", width=115, stretch=False)
        self.safety_tree.column("recovery", width=75, stretch=False)
        self.safety_tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(frame, orient="vertical", command=self.safety_tree.yview).grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(page)
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="Reveal in Finder", command=self._reveal_safety_selected).pack(side="left")
        ttk.Button(actions, text="Restore related run…", command=self._restore_safety_selected).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open in Dedupe", command=self._dedupe_safety_selected).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Move to Trash…", command=self._trash_safety_selected).pack(side="right")

    def _sync_safety_root_from_context(self) -> None:
        if self.safety_root_var.get().strip():
            return
        current = self.path_var.get().strip()
        if current:
            self.safety_root_var.set(current)
        elif self.schedule_panel.cfg.folders:
            self.safety_root_var.set(self.schedule_panel.cfg.folders[0].path)

    def _refresh_safety_center(self) -> None:
        for row in self.safety_tree.get_children():
            self.safety_tree.delete(row)
        raw = self.safety_root_var.get().strip()
        if raw:
            roots = [normalize_folder_input(raw)]
        else:
            roots = [normalize_folder_input(job.path) for job in self.schedule_panel.cfg.folders]
        self._safety_items = scan_safety_items(roots)
        total_files = sum(item.files for item in self._safety_items)
        total_size = sum(item.size_bytes for item in self._safety_items)
        for index, item in enumerate(self._safety_items):
            recovery = "Available" if manifest_for_item(item) else "Not found"
            self.safety_tree.insert(
                "",
                "end",
                iid=f"safety-{index}",
                values=(
                    item.container,
                    item.path.name,
                    _short_path(str(item.base), 38),
                    item.files,
                    _human_size(item.size_bytes),
                    item.display_modified,
                    recovery,
                ),
            )
        self.safety_status_var.set(
            f"{len(self._safety_items)} held item{'s' if len(self._safety_items) != 1 else ''} · {total_files} file{'s' if total_files != 1 else ''} · {_human_size(total_size)}"
        )

    def _selected_safety_item(self) -> Optional[SafetyItem]:
        selection = self.safety_tree.selection()
        if not selection:
            messagebox.showwarning("Safety center", "Select an item first.")
            return None
        try:
            return self._safety_items[int(selection[0].split("-")[-1])]
        except (ValueError, IndexError):
            return None

    def _reveal_safety_selected(self) -> None:
        item = self._selected_safety_item()
        if item is not None and not reveal_in_file_manager(item.path):
            messagebox.showerror("Safety center", "The selected item could not be revealed.")

    def _restore_safety_selected(self) -> None:
        item = self._selected_safety_item()
        if item is None:
            return
        manifest = manifest_for_item(item)
        if manifest is None:
            messagebox.showinfo("Recovery unavailable", "No recovery manifest was found for this item.")
            return
        if not messagebox.askyesno(
            "Restore related run",
            f"Restore the full run associated with:\n{item.path.name}\n\nBackup: {manifest.name}\n\nExisting files will not be overwritten.",
        ):
            return
        ok, message = restore_item_run(item)
        messagebox.showinfo("Restore complete" if ok else "Restore failed", message)
        self._refresh_safety_center()

    def _trash_safety_selected(self) -> None:
        item = self._selected_safety_item()
        if item is None:
            return
        if not messagebox.askyesno(
            "Move to Trash",
            f"Move this held item to the macOS Trash?\n\n{item.path}\n\nIt will not be permanently deleted.",
        ):
            return
        ok, message = move_to_trash(item.path)
        messagebox.showinfo("Moved to Trash" if ok else "Trash failed", message)
        self._refresh_safety_center()

    def _dedupe_safety_selected(self) -> None:
        item = self._selected_safety_item()
        if item is None:
            return
        self.safety_status_var.set("Opening Dedupe and handing off the selected item…")

        def work() -> None:
            ok, message = handoff_to_dedupe([item.path])
            self.root.after(
                0,
                lambda: (
                    self.safety_status_var.set(message),
                    messagebox.showinfo("Dedupe" if ok else "Dedupe handoff failed", message),
                ),
            )

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # Advanced settings and standalone rename
    # ------------------------------------------------------------------

    def _build_advanced_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(6, weight=1)
        ttk.Label(page, text="Advanced settings", style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(page, text="Specialized tools and technical diagnostics.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 18))

        rename_group = ttk.LabelFrame(page, text="Random rename", padding=14)
        rename_group.grid(row=2, column=0, sticky="ew")
        rename_group.columnconfigure(1, weight=1)
        ttk.Label(rename_group, text="Folder").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(rename_group, textvariable=self.rename_path_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(rename_group, text="Browse…", command=lambda: self._browse(self.rename_path_var, "Choose folder for random renaming")).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(
            rename_group,
            text="Random filenames can be restored from the backup created by the run.",
            style="Warning.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        options = ttk.Frame(rename_group)
        options.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Checkbutton(options, text="Include subfolders", variable=self.rename_recursive_var).pack(side="left")
        ttk.Checkbutton(options, text="Include hidden files", variable=self.rename_hidden_var).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(options, text="Skip already-randomized names", variable=self.rename_skip_randomly_renamed_var).pack(side="left", padx=(14, 0))
        rename_actions = ttk.Frame(rename_group)
        rename_actions.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.rename_preview_button = ttk.Button(rename_actions, text="Preview rename", command=lambda: self._run_rename(True))
        self.rename_preview_button.pack(side="left")
        self.rename_run_button = ttk.Button(rename_actions, text="Rename files…", command=lambda: self._run_rename(False))
        self.rename_run_button.pack(side="left", padx=(8, 0))

        ttk.Label(page, text="Technical details", style="SectionTitle.TLabel").grid(row=3, column=0, sticky="w", pady=(22, 8))
        self.technical_paths_var = tk.StringVar(
            value=f"Configuration: {self.schedule_panel.config_path}\nScheduler log: {service_log_path()}"
        )
        ttk.Label(page, textvariable=self.technical_paths_var, style="Muted.TLabel", justify="left").grid(row=4, column=0, sticky="w")
        details_header = ttk.Frame(page)
        details_header.grid(row=5, column=0, sticky="ew", pady=(14, 6))
        ttk.Label(details_header, text="Command details", style="SectionTitle.TLabel").pack(side="left")
        ttk.Button(details_header, text="Clear", command=lambda: self.details_out.delete("1.0", "end")).pack(side="right")
        self.details_out = scrolledtext.ScrolledText(page, wrap="word", height=14, font=("Menlo", 10))
        self.details_out.grid(row=6, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    # Commands and results
    # ------------------------------------------------------------------

    def _browse(self, var: tk.StringVar, title: str) -> None:
        selected = filedialog.askdirectory(title=title)
        if selected:
            var.set(selected)

    def _browse_file(
        self,
        var: tk.StringVar,
        title: str,
        filetypes: tuple[tuple[str, str], ...] = (("All files", "*"),),
    ) -> None:
        selected = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if selected:
            var.set(selected)

    def _resolve_folder(self, var: tk.StringVar, title: str = "Folder") -> Path | None:
        raw = var.get().strip()
        if not raw:
            messagebox.showwarning(title, "Choose a folder first.")
            return None
        path = normalize_folder_input(raw)
        if not path.is_dir():
            messagebox.showerror(title, f"Not a directory:\n{path}")
            return None
        var.set(str(path))
        return path

    def _build_organize_cmd(self, dry_run: bool, base: Path) -> list[str]:
        cmd = [
            sys.executable,
            str(_helper_script()),
            "--path",
            str(base),
            "--strategy",
            self.strategy_var.get(),
            "--profile",
            self.profile_var.get(),
            "--normalize",
            self.normalize_var.get(),
            "--recursive" if self.recursive_var.get() else "--no-recursive",
            "--include-hidden" if self.hidden_var.get() else "--no-include-hidden",
            "--collect-empty-dirs" if self.collect_empty_var.get() else "--no-collect-empty-dirs",
            "--backup",
        ]
        if self.exclude_defaults_var.get():
            cmd.append("--exclude-defaults")
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
        if self.verbose_var.get():
            cmd.append("--verbose")
        rules_file = self.rules_file_var.get().strip()
        archive_root = self.archive_root_var.get().strip()
        archive_mapping = self.archive_mapping_var.get().strip()
        if rules_file:
            cmd.extend(["--rules", rules_file])
        if self.unmatched_mode_var.get() != FALLBACK_BUCKET:
            cmd.extend(["--unmatched", self.unmatched_mode_var.get()])
        if archive_root:
            cmd.extend(["--archive-root", archive_root])
            if archive_mapping:
                cmd.extend(["--archive-mapping", archive_mapping])
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run_organize(self, dry_run: bool) -> None:
        base = self._resolve_folder(self.path_var)
        if base is None:
            return
        rules_file = self.rules_file_var.get().strip()
        archive_root = self.archive_root_var.get().strip()
        archive_mapping = self.archive_mapping_var.get().strip()
        if rules_file and archive_root:
            messagebox.showerror("Routing settings", "Use either a rules file or the Downloader Archive root, not both.")
            return
        if archive_mapping and not archive_root:
            messagebox.showerror("Routing settings", "Choose an Archive root before using an archive folder mapping.")
            return
        if not dry_run and self._last_preview_fingerprint != self._preview_fingerprint():
            messagebox.showwarning("Preview required", "Preview the current folder and settings before organizing.")
            return
        if not dry_run:
            warning = "\n\nFilenames will be randomized." if self.rename_after_organize_var.get() else ""
            if not messagebox.askyesno(
                "Organize files",
                f"Organize the previewed changes in:\n{base}{warning}\n\nA recovery backup will be created.",
            ):
                return

        if self.expand_subfolders_var.get():
            folders = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name.casefold())
            if not folders:
                messagebox.showinfo("Organize", "No immediate subfolders were found.")
                return
        else:
            folders = [base]
        jobs = [(folder.name, self._build_organize_cmd(dry_run, folder)) for folder in folders]
        self._start_jobs(jobs, "preview" if dry_run else "organize")

    def _build_rename_cmd(self, dry_run: bool, base: Path) -> list[str]:
        cmd = [sys.executable, str(_rename_script()), "--path", str(base)]
        cmd.append("--recursive" if self.rename_recursive_var.get() else "--no-recursive")
        cmd.append("--include-hidden" if self.rename_hidden_var.get() else "--no-include-hidden")
        if self.rename_skip_randomly_renamed_var.get():
            cmd.append("--skip-randomly-renamed")
        if self.rename_verbose_var.get():
            cmd.append("--verbose")
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run_rename(self, dry_run: bool) -> None:
        base = self._resolve_folder(self.rename_path_var, "Random rename")
        if base is None:
            return
        if not dry_run and not messagebox.askyesno(
            "Random rename",
            f"Replace filenames with random names in:\n{base}\n\nA recovery backup will be created.",
        ):
            return
        self._start_jobs([(base.name, self._build_rename_cmd(dry_run, base))], "rename-preview" if dry_run else "rename")

    def _add_to_schedule(self) -> None:
        base = self._resolve_folder(self.path_var)
        if base is None:
            return
        added = self.schedule_panel.add_folder_path(
            str(base),
            recursive=self.recursive_var.get(),
            strategy=self.strategy_var.get(),
            normalize=self.normalize_var.get(),
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
            rules_file=self.rules_file_var.get().strip() or None,
            unmatched_mode=self.unmatched_mode_var.get(),
            archive_root=self.archive_root_var.get().strip() or None,
            archive_mapping=self.archive_mapping_var.get().strip() or None,
        )
        if added:
            self._show_page("watched")

    def _restore_latest(self) -> None:
        base = self._resolve_folder(self.path_var, "Restore")
        if base is None:
            return
        manifests = list_manifests(base)
        if not manifests:
            messagebox.showinfo("Restore", "No recovery backups were found for this folder.")
            return
        self._restore_manifest(str(manifests[0]))

    def _restore_manifest(self, manifest: str) -> None:
        path = Path(manifest)
        if not path.is_file():
            messagebox.showinfo("Undo unavailable", "The recovery backup for this run is no longer available.")
            return
        if not messagebox.askyesno("Undo run", f"Restore files from this backup?\n{path.name}"):
            return
        self._start_jobs([("Undo", [sys.executable, str(_restore_script()), str(path)])], "restore")

    def _start_jobs(self, jobs: list[tuple[str, list[str]]], target: str) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showwarning("Busy", "Another command is already running.")
            return
        self._cancel_requested = False
        self._active_target = target
        self._set_busy(True)
        self._worker = threading.Thread(target=self._worker_main, args=(jobs, target), daemon=True)
        self._worker.start()
        self.root.after(100, self._poll_queue)

    def _worker_main(self, jobs: list[tuple[str, list[str]]], target: str) -> None:
        results: list[dict] = []
        for label, cmd in jobs:
            if self._cancel_requested:
                break
            self._out_queue.put(("command", label, cmd))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except OSError as exc:
                results.append({"label": label, "stdout": "", "stderr": str(exc), "returncode": 1})
                continue
            self._active_proc = proc
            try:
                stdout, stderr = proc.communicate(timeout=3600)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                stderr = (stderr + "\nTimed out after 1 hour.").strip()
            finally:
                self._active_proc = None
            result = {"label": label, "stdout": stdout, "stderr": stderr, "returncode": proc.returncode}
            results.append(result)
            self._out_queue.put(("result", result))
        self._out_queue.put(("done", target, results))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._out_queue.get_nowait()
                kind = item[0]
                if kind == "command":
                    _kind, label, cmd = item
                    self._append_details(f"\n--- {label} ---\n$ {' '.join(cmd)}\n\n")
                elif kind == "result":
                    result = item[1]
                    if result["stderr"]:
                        self._append_details(result["stderr"].rstrip() + "\n")
                    if result["stdout"]:
                        self._append_details(result["stdout"].rstrip() + "\n")
                elif kind == "done":
                    _kind, target, results = item
                    self._set_busy(False)
                    self._handle_job_done(target, results)
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_job_done(self, target: str, results: list[dict]) -> None:
        if self._cancel_requested:
            self.preview_subtitle_var.set("Cancelled. No additional changes will be started.")
            return
        if target == "preview":
            self._handle_preview_results(results)
        elif target == "organize":
            self._handle_organize_results(results)
        elif target == "restore":
            ok = all(r["returncode"] == 0 for r in results)
            messagebox.showinfo("Undo complete" if ok else "Undo failed", "Files were restored." if ok else "Open Advanced settings for command details.")
            self._refresh_history()
            self._refresh_overview_once()
        elif target.startswith("rename"):
            ok = all(r["returncode"] == 0 for r in results)
            verb = "Preview complete" if target == "rename-preview" else "Rename complete"
            messagebox.showinfo(verb, "Review command details below." if ok else "The command failed. Review command details below.")
            self._show_page("advanced")

    @staticmethod
    def _parse_summary(stdout: str) -> dict | None:
        start = stdout.find("{")
        if start == -1:
            return None
        try:
            value = json.loads(stdout[start:])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def _handle_preview_results(self, results: list[dict]) -> None:
        if any(r["returncode"] != 0 for r in results):
            self.preview_title_var.set("Preview failed")
            self.preview_subtitle_var.set("No files were changed. Open Advanced settings for details.")
            self.preview_warning_var.set("")
            return
        summaries = [self._parse_summary(r["stdout"]) for r in results]
        if any(summary is None for summary in summaries):
            self.preview_title_var.set("Preview could not be read")
            self.preview_subtitle_var.set("No files were changed. Open Advanced settings for details.")
            return
        clean = [summary for summary in summaries if summary is not None]
        moved = sum(int(summary.get("files_moved") or 0) for summary in clean)
        empty_dirs = sum(int((summary.get("empty_folder_collection") or {}).get("folders_moved") or 0) for summary in clean)
        normalized = sum(int((summary.get("normalization") or {}).get("items_moved_in_merges") or 0) for summary in clean)
        collisions = sum(int(summary.get("name_collisions_resolved") or 0) for summary in clean)
        categories: Counter[str] = Counter()
        planned: list[tuple[str, str, str]] = []
        needs_review = 0
        external_moves = 0
        for summary in clean:
            category_map = summary.get("moved_by_category")
            if isinstance(category_map, dict):
                categories.update({str(k): int(v or 0) for k, v in category_map.items()})
            for move in summary.get("planned_moves") or []:
                if isinstance(move, dict):
                    planned.append((str(move.get("from", "")), str(move.get("to", "")), str(move.get("reason", ""))))
            routing = summary.get("routing") or {}
            if isinstance(routing, dict):
                needs_review += int(routing.get("needs_review_files") or 0)
                external_moves += int(routing.get("external_moves") or 0)

        self._preview_change_count = moved + empty_dirs + normalized
        self.preview_title_var.set(f"Ready to organize {moved} file{'s' if moved != 1 else ''}")
        self.preview_subtitle_var.set("Previewed just now. No files have been changed.")
        counts = [f"{name} {count}" for name, count in categories.items() if count]
        counts.append(f"Total {moved}")
        if empty_dirs:
            counts.append(f"Empty folders staged {empty_dirs}")
        self.preview_counts_var.set("   ·   ".join(counts))
        warnings = []
        if collisions:
            warnings.append(f"{collisions} name collision{'s' if collisions != 1 else ''} will be resolved without overwriting")
        if self.rename_after_organize_var.get() and moved:
            warnings.append(f"{moved} file{'s' if moved != 1 else ''} will receive random filenames")
        if needs_review:
            warnings.append(f"{needs_review} unmatched file{'s' if needs_review != 1 else ''} will be held in {NEEDS_REVIEW_DIR_NAME}")
        if external_moves:
            warnings.append(f"{external_moves} file{'s' if external_moves != 1 else ''} will move into the selected Archive root")
        self.preview_warning_var.set("   ·   ".join(warnings))

        for item in self.planned_tree.get_children():
            self.planned_tree.delete(item)
        for source, destination, reason in planned[:200]:
            self.planned_tree.insert("", "end", values=(source, destination, reason))
        if not planned:
            self.planned_tree.insert("", "end", values=("No file moves needed", "Folder is already organized", ""))

        self._last_preview_fingerprint = self._preview_fingerprint()
        state = "normal" if self._preview_change_count > 0 else "disabled"
        self.organize_button.configure(state=state, text=f"Organize {moved} file{'s' if moved != 1 else ''}")

    def _handle_organize_results(self, results: list[dict]) -> None:
        failed = [r for r in results if r["returncode"] != 0]
        if failed:
            self.preview_title_var.set("Organization did not finish")
            self.preview_subtitle_var.set("Review Advanced settings for details. Existing files were not overwritten.")
            return
        summaries = [self._parse_summary(r["stdout"]) for r in results]
        clean = [summary for summary in summaries if summary is not None]
        moved = sum(int(summary.get("files_moved") or 0) for summary in clean)
        manifests = [str(summary.get("backup_manifest")) for summary in clean if summary.get("backup_manifest")]
        self._last_manifest = manifests[-1] if manifests else None
        self.preview_title_var.set(f"Organized {moved} file{'s' if moved != 1 else ''}")
        self.preview_subtitle_var.set("Completed successfully. Undo is available from Run history while the backup remains.")
        self.preview_warning_var.set("")
        self.organize_button.configure(state="disabled", text="Organization complete")
        self._last_preview_fingerprint = None

        for result, summary in zip(results, summaries):
            if summary is None:
                continue
            append_history_entry(
                {
                    "path": summary.get("target") or self.path_var.get(),
                    "label": "manual organize",
                    "ok": True,
                    "files_moved": summary.get("files_moved"),
                    "moved_by_category": summary.get("moved_by_category"),
                    "name_collisions_resolved": summary.get("name_collisions_resolved"),
                    "duplicates_moved": (summary.get("duplicates") or {}).get("files_moved"),
                    "empty_dirs_staged": (summary.get("empty_folder_collection") or {}).get("folders_moved"),
                    "needs_review_files": (summary.get("routing") or {}).get("needs_review_files"),
                    "external_moves": (summary.get("routing") or {}).get("external_moves"),
                    "matched_by_rule": (summary.get("routing") or {}).get("matched_by_rule"),
                    "backup_manifest": summary.get("backup_manifest"),
                }
            )
        self._refresh_history()
        self._refresh_overview_once()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.preview_button, self.rename_preview_button, self.rename_run_button):
            widget.configure(state=state)
        if busy:
            self.watch_folder_button.pack_forget()
            self.restore_button.pack_forget()
            self.cancel_button.configure(state="normal")
            self.cancel_button.pack(side="right")
            self.organize_button.configure(state="disabled")
        else:
            self.cancel_button.pack_forget()
            self.watch_folder_button.pack(side="left", padx=(8, 0))
            self.restore_button.pack(side="left", padx=(8, 0))

    def _cancel(self) -> None:
        self._cancel_requested = True
        proc = self._active_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _append_details(self, text: str) -> None:
        self.details_out.insert("end", text)
        self.details_out.see("end")

    def _on_close(self) -> None:
        self._cancel()
        self.schedule_panel.shutdown()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = CommandCenterApp(root)
    if "--page" in sys.argv:
        try:
            app._show_page(sys.argv[sys.argv.index("--page") + 1])
        except (IndexError, KeyError):
            pass
    for flag in ("--path", "--folder"):
        if flag in sys.argv:
            try:
                app.path_var.set(sys.argv[sys.argv.index(flag) + 1])
            except IndexError:
                pass
            break
    if "--preview" in sys.argv:
        root.after(250, lambda: app._run_organize(True))
    root.mainloop()


if __name__ == "__main__":
    main()
