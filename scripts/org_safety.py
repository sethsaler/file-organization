#!/usr/bin/env python3
"""Safety Center helpers for quarantine review, recovery, Trash, and Dedupe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from org_manifest import (
    FOR_DELETION_DIR_NAME,
    Manifest,
    ManifestEntry,
    list_manifests,
    restore_from_manifest,
    write_manifest_files,
)
from org_rules import NEEDS_REVIEW_DIR_NAME


DUPLICATES_DIR_NAME = "Duplicates"
SAFETY_DIR_NAMES = (NEEDS_REVIEW_DIR_NAME, FOR_DELETION_DIR_NAME, DUPLICATES_DIR_NAME)
DEDUPE_URL = "http://127.0.0.1:8765"


@dataclass(frozen=True)
class SafetyItem:
    base: Path
    container: str
    path: Path
    files: int
    size_bytes: int
    modified_at: float

    @property
    def display_modified(self) -> str:
        try:
            return datetime.fromtimestamp(self.modified_at).astimezone().strftime("%b %-d, %-I:%M %p")
        except (OSError, ValueError, OverflowError):
            return "Unknown"


def _measure(path: Path) -> Tuple[int, int, float]:
    files = 0
    size = 0
    modified = 0.0
    try:
        initial = os.lstat(path)
        modified = initial.st_mtime
    except OSError:
        return 0, 0, 0.0
    if path.is_file() and not path.is_symlink():
        return 1, max(0, initial.st_size), modified
    for root, dirs, names in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
        for name in names:
            candidate = root_path / name
            try:
                stat = os.lstat(candidate)
            except OSError:
                continue
            if not candidate.is_symlink():
                files += 1
                size += max(0, int(stat.st_size))
            modified = max(modified, stat.st_mtime)
    return files, size, modified


def scan_safety_items(roots: Iterable[Path], *, limit: int = 1000) -> List[SafetyItem]:
    items: List[SafetyItem] = []
    seen = set()
    for raw_root in roots:
        try:
            root = raw_root.expanduser().resolve()
        except OSError:
            continue
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        for container_name in SAFETY_DIR_NAMES:
            container = root / container_name
            if not container.is_dir():
                continue
            try:
                children = sorted(container.iterdir(), key=lambda p: p.name.casefold())
            except OSError:
                continue
            for child in children:
                files, size, modified = _measure(child)
                items.append(
                    SafetyItem(
                        base=root,
                        container=container_name,
                        path=child,
                        files=files,
                        size_bytes=size,
                        modified_at=modified,
                    )
                )
                if len(items) >= limit:
                    return sorted(items, key=lambda item: item.modified_at, reverse=True)
    return sorted(items, key=lambda item: item.modified_at, reverse=True)


def _is_safety_item(path: Path) -> bool:
    folded = {name.casefold() for name in SAFETY_DIR_NAMES}
    return any(parent.name.casefold() in folded for parent in path.parents)


def reveal_in_file_manager(path: Path) -> bool:
    target = path.expanduser()
    if not target.exists():
        return False
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
        return True
    except OSError:
        return False


def move_to_trash(path: Path) -> Tuple[bool, str]:
    """Move a quarantine item to the operating-system Trash; never unlink it."""
    target = path.expanduser().resolve()
    if not target.exists():
        return False, "Item no longer exists"
    if not _is_safety_item(target):
        return False, "Only items inside Needs Review, For Deletion, or Duplicates can be trashed"
    try:
        if sys.platform == "darwin":
            proc = subprocess.run(
                [
                    "osascript",
                    "-e",
                    "on run argv",
                    "-e",
                    'tell application "Finder" to delete POSIX file (item 1 of argv)',
                    "-e",
                    "end run",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                return False, (proc.stderr or proc.stdout or "Finder could not move the item to Trash").strip()
        elif sys.platform.startswith("win"):
            return False, "Trash integration is not available on Windows yet"
        else:
            proc = subprocess.run(["gio", "trash", str(target)], capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return False, (proc.stderr or "gio trash failed").strip()
        return True, "Moved to Trash"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def _entry_absolute(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else base / candidate


def manifest_for_item(item: SafetyItem) -> Optional[Path]:
    """Newest backup manifest whose destination contains the selected item."""
    for manifest_path in list_manifests(item.base, limit=100):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        base = Path(str(data.get("base_path") or item.base)).expanduser()
        for group in ("file_moves", "empty_dir_moves"):
            for entry in data.get(group, []):
                if not isinstance(entry, dict):
                    continue
                try:
                    destination = _entry_absolute(base, str(entry.get("to") or "")).resolve()
                except OSError:
                    continue
                if destination == item.path or item.path in destination.parents or destination in item.path.parents:
                    return manifest_path
    return None


def original_source_for_review(base: Path, review_item: Path) -> Path:
    target = review_item.expanduser().resolve()
    for manifest_path in list_manifests(base, limit=100):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        manifest_base = Path(str(data.get("base_path") or base)).expanduser()
        for entry in data.get("file_moves", []):
            if not isinstance(entry, dict):
                continue
            destination = _entry_absolute(manifest_base, str(entry.get("to") or ""))
            try:
                same = destination.resolve() == target
            except OSError:
                same = destination == target
            if same:
                return _entry_absolute(manifest_base, str(entry.get("from") or review_item.name))
    return review_item


def approve_review_item(base: Path, item_path: Path, destination: str) -> Tuple[Path, Optional[str]]:
    root = base.expanduser().resolve()
    item = item_path.expanduser().resolve()
    review_root = (root / NEEDS_REVIEW_DIR_NAME).resolve()
    if review_root not in item.parents:
        raise ValueError("Selected item is not inside this folder's Needs Review queue")
    relative_destination = Path(destination.strip())
    if not destination.strip() or relative_destination.is_absolute() or ".." in relative_destination.parts:
        raise ValueError("Destination must be a folder inside the selected organizer root")
    destination_dir = (root / relative_destination).resolve()
    if root != destination_dir and root not in destination_dir.parents:
        raise ValueError("Destination escapes the organizer root")
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / item.name
    index = 1
    while target.exists():
        target = destination_dir / f"{item.stem}_{index}{item.suffix}"
        index += 1
    shutil.move(str(item), str(target))
    manifest = Manifest(
        created_at=datetime.now().isoformat(),
        base_path=str(root),
        mode="review",
        strategy="approved",
        normalize="none",
        profile="review-queue",
        file_moves=[
            ManifestEntry(
                from_path=str(item.relative_to(root)),
                to_path=str(target.relative_to(root)),
            )
        ],
    )
    info = write_manifest_files(
        root,
        manifest,
        helper_script=Path(__file__).resolve().parent / "organize_by_filetype.py",
        dry_run=False,
        create_backup=True,
    )
    return target, info.get("manifest") if info else None


def restore_item_run(item: SafetyItem) -> Tuple[bool, str]:
    manifest = manifest_for_item(item)
    if manifest is None:
        return False, "No recovery manifest was found for this item"
    ok = restore_from_manifest(str(manifest))
    return ok, f"Restored from {manifest.name}" if ok else "Restore failed"


def default_dedupe_launcher() -> Optional[Path]:
    configured = os.environ.get("FILE_ORG_DEDUPE_LAUNCHER", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            Path.home() / "Documents" / "GitHub" / "dedupe" / "Dedupe.command",
            Path.home() / "Documents" / "GitHub" / "dedupe" / "launchers" / "Dedupe.command",
        ]
    )
    return next((path for path in candidates if path.is_file()), None)


def _dedupe_request(path: str, *, method: str = "GET", body: Optional[dict] = None, timeout: float = 2.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        DEDUPE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def handoff_to_dedupe(paths: Sequence[Path], *, wait_seconds: float = 20.0) -> Tuple[bool, str]:
    roots = [str(path.expanduser().resolve()) for path in paths if path.expanduser().exists()]
    if not roots:
        return False, "No existing folders were selected for Dedupe"

    running = False
    try:
        _dedupe_request("/api/status")
        running = True
    except (OSError, ValueError, urllib.error.URLError):
        launcher = default_dedupe_launcher()
        if launcher is None:
            return False, "Dedupe launcher was not found"
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(launcher)])
            else:
                subprocess.Popen([str(launcher)])
        except OSError as exc:
            return False, str(exc)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while time.monotonic() < deadline:
            try:
                _dedupe_request("/api/status")
                running = True
                break
            except (OSError, ValueError, urllib.error.URLError):
                time.sleep(0.4)

    if not running:
        return True, "Dedupe was launched; choose the folder when it finishes opening"
    try:
        _dedupe_request(
            "/api/scan",
            method="POST",
            body={
                "paths": roots,
                "exact": True,
                "similar": True,
                "include_images": True,
                "include_gifs": True,
                "include_videos": True,
            },
            timeout=10.0,
        )
        if sys.platform == "darwin":
            subprocess.Popen(["open", DEDUPE_URL])
        return True, "Dedupe opened and started scanning the selected folder"
    except urllib.error.HTTPError as exc:
        if sys.platform == "darwin":
            subprocess.Popen(["open", DEDUPE_URL])
        if exc.code == 409:
            return True, "Dedupe is already busy; its current review was opened"
        return False, f"Dedupe rejected the handoff: HTTP {exc.code}"
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return False, f"Dedupe opened, but the scan handoff failed: {exc}"
