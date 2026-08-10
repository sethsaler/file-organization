#!/usr/bin/env python3
"""Standalone window for scheduled folder organization.

The same scheduler is built into the main app: open `tinker_gui.py` and use the Schedule tab.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from schedule_panel import SchedulePanel


def main() -> None:
    root = tk.Tk()
    root.title("Organize on a schedule")
    root.minsize(640, 560)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    panel = SchedulePanel(root, root, embedded=False)
    panel.grid(row=0, column=0, sticky="nsew")

    def on_close() -> None:
        panel.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()



if __name__ == "__main__":
    main()
