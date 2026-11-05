#!/usr/bin/env python3
"""Backup manifests and restore."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from org_paths import available_path, move_to_available

ORGANIZER_DIR_NAME = ".organizer"
FOR_DELETION_DIR_NAME = "For Deletion"


@dataclass
class ManifestEntry:
    from_path: str
    to_path: str


@dataclass
class Manifest:
    version: int = 2
    created_at: str = ""
    base_path: str = ""
    mode: str = ""
    strategy: str = ""
    normalize: str = ""
    profile: str = ""
    file_moves: List[ManifestEntry] = field(default_factory=list)
    empty_dir_moves: List[ManifestEntry] = field(default_factory=list)
    empty_dirs_removed: List[str] = field(default_factory=list)
    external_destinations: bool = False


def list_manifests(base: Path, limit: int = 20) -> List[Path]:
    backup_dir = base / ORGANIZER_DIR_NAME
    if not backup_dir.is_dir():
        return []
    return sorted(backup_dir.glob("backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def cleanup_old_manifests(base: Path, days_to_keep: int = 7) -> int:
    """Remove manifest files older than days_to_keep days. Returns count of files removed."""
    import time
    backup_dir = base / ORGANIZER_DIR_NAME
    if not backup_dir.is_dir():
        return 0
    
    cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
    removed_count = 0
    
    for manifest_file in backup_dir.glob("backup_*.json"):
        try:
            if manifest_file.stat().st_mtime < cutoff_time:
                try:
                    data = json.loads(manifest_file.read_text())
                except (OSError, ValueError):
                    continue
                if data.get("status") in {"in-progress", "failed", "interrupted"}:
                    continue
                journal = manifest_file.with_name(manifest_file.name.replace("backup_", "journal_")).with_suffix(".jsonl")
                journal.unlink(missing_ok=True)
                manifest_file.with_suffix(".command").unlink(missing_ok=True)
                manifest_file.with_suffix(".lock").unlink(missing_ok=True)
                manifest_file.unlink()
                removed_count += 1
        except OSError:
            pass
    
    # Also clean up associated restore scripts
    for restore_script in backup_dir.glob("restore_*.sh"):
        try:
            if restore_script.stat().st_mtime < cutoff_time:
                restore_script.unlink()
        except OSError:
            pass
    
    for restore_command in backup_dir.glob("Restore from *.command"):
        try:
            if restore_command.stat().st_mtime < cutoff_time:
                restore_command.unlink()
        except OSError:
            pass
    
    return removed_count


def write_manifest_files(
    base: Path,
    manifest: Manifest,
    *,
    helper_script: Path,
    dry_run: bool,
    create_backup: bool,
    manifest_path: Optional[Path] = None,
) -> Optional[Dict[str, str]]:
    if dry_run or not create_backup:
        return None

    backup_dir = base / ORGANIZER_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Microseconds prevent two quick manual/review actions from overwriting the
    # same recovery manifest within one second.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    manifest_path = manifest_path or backup_dir / f"backup_{timestamp}.json"

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
        "external_destinations": manifest.external_destinations,
        "status": "complete",
    }
    _atomic_json(manifest_path, data)

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


def _validate_move_list(name: str, items: Any) -> Optional[str]:
    if not isinstance(items, list):
        return f"{name} must be a JSON array"
    for i, entry in enumerate(items):
        if not isinstance(entry, dict):
            return f"{name}[{i}] must be a JSON object"
        for key in ("from", "to"):
            if key not in entry:
                return f"{name}[{i}] missing {key!r} key"
            v = entry[key]
            if not isinstance(v, str):
                return f"{name}[{i}] {key!r} must be a string"
    return None


def restore_from_manifest(manifest_path: str) -> bool:
    manifest_file = Path(manifest_path)
    if not manifest_file.exists():
        print(json.dumps({"error": f"Manifest not found: {manifest_path}"}, indent=2))
        return False
    try:
        data = read_manifest_data(manifest_file)
    except (OSError, ValueError) as e:
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

    for key in ("empty_dir_moves", "file_moves"):
        err = _validate_move_list(key, data.get(key, []))
        if err:
            print(json.dumps({"error": err}, indent=2))
            return False

    if is_manifest_active(manifest_file):
        print(json.dumps({"error": "This run is still active; stop it before restoring"}))
        return False

    restored_files = restored_dirs = collisions = 0
    allow_external = bool(data.get("external_destinations")) and int(data.get("version", 1)) >= 2

    def manifest_path(value: str) -> Path:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            if not allow_external:
                raise ValueError("Manifest contains an absolute path without external-destination metadata")
            return candidate
        if any(part == ".." for part in candidate.parts):
            raise ValueError("Manifest path escapes its base folder")
        result = base / candidate
        try:
            result.resolve().relative_to(base.resolve())
        except ValueError:
            raise ValueError("Manifest path escapes its base folder through a symlink")
        return result

    try:
        for key in ("empty_dir_moves", "file_moves"):
            for entry in data.get(key, []):
                manifest_path(entry["to"])
                manifest_path(entry["from"])
        removed_paths = []
        removed = data.get("empty_dirs_removed", [])
        if not isinstance(removed, list):
            raise ValueError("empty_dirs_removed must be an array")
        for value in removed:
            if not isinstance(value, str) or Path(value).is_absolute():
                raise ValueError("Invalid removed-directory path")
            removed_paths.append(manifest_path(value))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return False

    restored_entries = set(data.get("restored_entries", []))
    skipped_changed = []
    restored_locations = data.get("restored_locations", {})
    operations = data.get("operations")
    if operations is None:
        operations = [{**entry, "kind": kind} for kind in ("file_moves", "empty_dir_moves")
                      for entry in data.get(kind, [])]
    for entry in reversed(operations):
        kind = entry["kind"]
        src, dst = manifest_path(entry["to"]), manifest_path(entry["from"])
        # Later operations may have restored an intermediate directory/name to
        # a collision suffix. Follow that actual location through the chain.
        for old, new in sorted(restored_locations.items(), key=lambda pair: len(pair[0]), reverse=True):
            try:
                relative = src.relative_to(Path(old))
            except ValueError:
                continue
            src = Path(new) / relative
            break
        token = entry.get("operation_id") or f"{kind}:{entry['from']}:{entry['to']}"
        if token in restored_entries or not os.path.lexists(src):
            continue
        expected = entry.get("identity")
        if expected and not _same_identity(src, expected):
            skipped_changed.append(str(src))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        actual = move_to_available(src, dst, restored=True)
        if actual != dst:
            collisions += 1
            restored_locations[str(dst)] = str(actual)
        if kind == "empty_dir_moves":
            restored_dirs += 1
        else:
            restored_files += 1
        restored_entries.add(token)
        data["restored_entries"] = sorted(restored_entries)
        data["restored_locations"] = restored_locations
        _atomic_json(manifest_file, data)

    for dir_path in reversed(removed_paths):
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
        "skipped_changed_paths": skipped_changed,
        "manifest_info": {
            "created_at": data.get("created_at", "unknown"),
            "profile": data.get("profile", "standard"),
        },
    }, indent=2))
    return not skipped_changed


def _atomic_json(path: Path, data: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".manifest-", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the name as well as its contents where directory fsync exists.
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _identity(path: Path) -> Optional[dict]:
    try:
        info = path.lstat()
        return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
                "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "directory": stat.S_ISDIR(info.st_mode)}
    except OSError:
        return None


def _same_identity(path: Path, expected: dict) -> bool:
    actual = _identity(path)
    if not actual:
        return False
    fields = ("device", "inode") if expected.get("directory") else ("device", "inode", "size", "mtime_ns")
    return all(actual.get(key) == expected.get(key) for key in fields)


def read_manifest_data(path: Path) -> dict:
    """Read a complete manifest or recover its durable operation journal."""
    data = json.loads(path.read_text(encoding="utf-8"))
    journal_name = data.get("journal_file")
    if not journal_name:
        return data
    # Journals always live beside their marker; never follow arbitrary paths.
    if Path(journal_name).name != journal_name:
        raise ValueError("Invalid recovery journal path")
    journal = path.parent / journal_name
    intents = {}
    completed = {}
    published_identities = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a crash may truncate only the final append
        if event.get("event") == "intent":
            intents[event["id"]] = event
        elif event.get("event") == "ready":
            published_identities[event["id"]] = event["identity"]
        elif event.get("event") == "done":
            completed[event["id"]] = event
    base = Path(data["base_path"])
    def stored(value):
        p = Path(value)
        try:
            return str(p.relative_to(base))
        except ValueError:
            return str(p)
    data["file_moves"], data["empty_dir_moves"], data["empty_dirs_removed"] = [], [], []
    data["operations"] = []
    for token, intent in intents.items():
        src = Path(intent["from"])
        if intent["kind"] == "rmdir":
            if token in completed or not os.path.lexists(src):
                data["empty_dirs_removed"].append(stored(src))
            continue
        dst = Path(intent["to"])
        done = completed.get(token)
        identity = done.get("identity") if done else published_identities.get(token, intent.get("identity"))
        # A same-volume rename is inferable even if killed before 'done'. A
        # cross-volume publication writes 'done' before removing its source.
        moved = done is not None or ((token in published_identities or not os.path.lexists(src)) and identity and _same_identity(dst, identity))
        if moved:
            key = "empty_dir_moves" if intent["kind"] == "directory" else "file_moves"
            entry = {"from": stored(src), "to": stored(dst), "identity": identity, "operation_id": token}
            data[key].append(entry)
            data["operations"].append({**entry, "kind": key})
    return data


class RecoveryJournal:
    """Write-ahead, fsynced recovery for every filesystem mutation in a run."""

    def __init__(self, base: Path, *, mode: str = "organize", strategy: str = "",
                 profile: str = "standard", external: bool = False) -> None:
        self.base = base.resolve()
        self.watch_mutations = {}
        directory = self.base / ORGANIZER_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]
        self.manifest_path = directory / f"backup_{stamp}.json"
        self.journal_path = directory / f"journal_{stamp}.jsonl"
        self._lock = self.manifest_path.with_suffix(".lock").open("a+")
        try:
            import fcntl
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        with self.journal_path.open("x", encoding="utf-8") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        self.metadata = {"version": 3, "created_at": datetime.now().isoformat(),
                         "base_path": str(self.base), "mode": mode, "strategy": strategy,
                         "profile": profile, "external_destinations": external,
                         "status": "in-progress", "journal_file": self.journal_path.name,
                         "file_moves": [], "empty_dir_moves": [], "empty_dirs_removed": []}
        _atomic_json(self.manifest_path, self.metadata)

    def _append(self, event: dict) -> None:
        with self.journal_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def move(self, source: Path, destination: Path, *, kind: str = "file") -> Path:
        token = None
        def before(src, dst):
            nonlocal token
            token = uuid.uuid4().hex
            self._append({"event": "intent", "id": token, "kind": kind,
                          "from": str(src.absolute()), "to": str(dst.absolute()), "identity": _identity(src)})
        def ready(staged):
            self._append({"event": "ready", "id": token, "identity": _identity(staged)})
        def after(dst):
            identity = _identity(dst)
            self._append({"event": "done", "id": token, "identity": identity})
            self.watch_mutations[str(dst.absolute())] = identity
        actual = move_to_available(source, destination, before_move=before, before_publish=ready, after_publish=after)
        self.watch_mutations[str(source.absolute())] = None
        self._record_parent_mutations(source, actual)
        return actual

    def hardlink_move(self, source: Path, canonical: Path, destination: Path) -> Path:
        from org_dupes import hash_file
        before_source, before_canonical = _identity(source), _identity(canonical)
        digest = hash_file(source)
        if digest is None or digest != hash_file(canonical):
            raise ValueError("Duplicate changed before hardlinking")
        if not _same_identity(source, before_source or {}) or not _same_identity(canonical, before_canonical or {}):
            raise ValueError("Duplicate changed while validating hardlink")
        while True:
            target = available_path(destination)
            token = uuid.uuid4().hex
            self._append({"event": "intent", "id": token, "kind": "file",
                          "from": str(source.absolute()), "to": str(target.absolute()), "identity": before_canonical})
            try:
                os.link(canonical, target)
                break
            except FileExistsError:
                continue
        # Linking changes ctime, not content/mtime. Check both files again.
        if not _same_identity(source, before_source or {}) or not _same_identity(canonical, before_canonical or {}):
            target.unlink()
            raise ValueError("Duplicate changed before removing source")
        ready_source, ready_canonical = source.stat(), canonical.stat()
        self._append({"event": "done", "id": token, "identity": _identity(target)})
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        current_source, current_canonical = source.stat(), canonical.stat()
        if (any(getattr(ready_source, key) != getattr(current_source, key) for key in fields)
                or any(getattr(ready_canonical, key) != getattr(current_canonical, key) for key in fields)):
            raise ValueError("Duplicate changed after journaling; both copies retained")
        source.unlink()
        self.watch_mutations[str(source.absolute())] = None
        self.watch_mutations[str(target.absolute())] = _identity(target)
        self._record_parent_mutations(source, target)
        return target

    def _record_parent_mutations(self, *paths) -> None:
        for path in paths:
            for parent in path.absolute().parents:
                if parent in self.base.parents:
                    break
                self.watch_mutations[str(parent)] = _identity(parent)
                if parent == self.base:
                    break

    def remove_empty_dir(self, path: Path) -> None:
        token = uuid.uuid4().hex
        self._append({"event": "intent", "id": token, "kind": "rmdir", "from": str(path.absolute())})
        path.rmdir()
        self._append({"event": "done", "id": token})
        self.watch_mutations[str(path.absolute())] = None
        self._record_parent_mutations(path)

    def finish(self, *, status: str = "complete") -> Dict[str, str]:
        try:
            data = read_manifest_data(self.manifest_path)
            data["status"] = status
            data.pop("journal_file", None)
            _atomic_json(self.manifest_path, data)
            restore = Path(__file__).resolve().parent / "restore_from_backup.py"
            import shlex
            command = " ".join(shlex.quote(str(p)) for p in (sys.executable, restore, self.manifest_path))
            script = self.manifest_path.with_suffix(".command")
            script.write_text("#!/bin/sh\nexec " + command + "\n", encoding="utf-8")
            script.chmod(0o755)
            return {"manifest": str(self.manifest_path), "restore_cli": command,
                    "restore_script": str(script), "restore_sh": str(script)}
        finally:
            self._lock.close()


def is_manifest_active(path: Path) -> bool:
    """A kernel-held lock distinguishes a running process from a crashed one."""
    try:
        import fcntl
        with path.with_suffix(".lock").open("r") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except (OSError, ImportError):
        return False


mutation_identity = _identity
