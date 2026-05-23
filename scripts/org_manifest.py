#!/usr/bin/env python3
"""Backup manifests and restore."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ORGANIZER_DIR_NAME = ".organizer"
FOR_DELETION_DIR_NAME = "For Deletion"


@dataclass
class ManifestEntry:
    from_path: str
    to_path: str


@dataclass
class Manifest:
    version: int = 1
    created_at: str = ""
    base_path: str = ""
    mode: str = ""
    strategy: str = ""
    normalize: str = ""
    profile: str = ""
    file_moves: List[ManifestEntry] = field(default_factory=list)
    empty_dir_moves: List[ManifestEntry] = field(default_factory=list)
    empty_dirs_removed: List[str] = field(default_factory=list)


def list_manifests(base: Path, limit: int = 20) -> List[Path]:
    backup_dir = base / ORGANIZER_DIR_NAME
    if not backup_dir.is_dir():
        return []
    return sorted(backup_dir.glob("backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def write_manifest_files(
    base: Path,
    manifest: Manifest,
    *,
    helper_script: Path,
    dry_run: bool,
    create_backup: bool,
) -> Optional[Dict[str, str]]:
    if dry_run or not create_backup:
        return None

    backup_dir = base / ORGANIZER_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = backup_dir / f"backup_{timestamp}.json"

    data = {
        "version": manifest.version,
        "created_at": manifest.created_at,
        "base_path": manifest.base_path,
        "mode": manifest.mode,
        "strategy": manifest.strategy,
        "normalize": manifest.normalize,
        "profile": manifest.profile,
        "file_moves": [{"from": m.from_path, "to": m.to_path} for m in manifest.file_moves],
        "empty_dir_moves": [{"from": m.from_path, "to": m.to_path} for m in manifest.empty_dir_moves],
        "empty_dirs_removed": manifest.empty_dirs_removed,
    }
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    restore_py = Path(__file__).resolve().parent / "restore_from_backup.py"
    restore_sh = backup_dir / f"restore_{timestamp}.sh"
    restore_sh.write_text(
        f'#!/usr/bin/env sh\nset -eu\nexec "{sys.executable}" "{restore_py.resolve()}" "{manifest_path.resolve()}"\n',
        encoding="utf-8",
    )
    restore_sh.chmod(0o755)

    restore_script = backup_dir / f"Restore from {timestamp}.command"
    restore_script.write_text(
        f"""#!/bin/bash
set -euo pipefail
MANIFEST="{manifest_path.resolve()}"
RESTORE_PY="{restore_py.resolve()}"
HELPER="{helper_script.resolve()}"
if [[ -f "$RESTORE_PY" ]]; then python3 "$RESTORE_PY" "$MANIFEST"; else python3 "$HELPER" --restore "$MANIFEST"; fi
read -r -p "Press Enter..." _
""",
        encoding="utf-8",
    )
    restore_script.chmod(0o755)

    return {
        "manifest": str(manifest_path),
        "restore_script": str(restore_script),
        "restore_sh": str(restore_sh),
        "restore_cli": f'{sys.executable} "{restore_py}" "{manifest_path}"',
    }


def restore_from_manifest(manifest_path: str) -> bool:
    manifest_file = Path(manifest_path)
    if not manifest_file.exists():
        print(json.dumps({"error": f"Manifest not found: {manifest_path}"}, indent=2))
        return False
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid manifest file: {e}"}, indent=2))
        return False

    try:
        base = Path(data["base_path"])
    except (KeyError, TypeError):
        print(json.dumps({"error": "Manifest missing or invalid base_path"}, indent=2))
        return False
    if not base.exists():
        print(json.dumps({"error": f"Base path not found: {base}"}, indent=2))
        return False

    restored_files = restored_dirs = collisions = 0
    for entry in reversed(data.get("empty_dir_moves", [])):
        src, dst = base / entry["to"], base / entry["from"]
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            collisions += 1
            dst = dst.parent / f"{dst.stem}_restored{dst.suffix}"
        shutil.move(str(src), str(dst))
        restored_dirs += 1

    for entry in reversed(data.get("file_moves", [])):
        src, dst = base / entry["to"], base / entry["from"]
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            collisions += 1
            dst = dst.parent / f"{dst.stem}_restored{dst.suffix}"
        shutil.move(str(src), str(dst))
        restored_files += 1

    for rel_dir in reversed(data.get("empty_dirs_removed", [])):
        dir_path = base / rel_dir
        if not dir_path.exists():
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    print(json.dumps({
        "restore_from": str(manifest_file),
        "base_path": str(base),
        "files_restored": restored_files,
        "directories_restored": restored_dirs,
        "collisions_renamed": collisions,
        "manifest_info": {
            "created_at": data.get("created_at", "unknown"),
            "profile": data.get("profile", "standard"),
        },
    }, indent=2))
    return True
