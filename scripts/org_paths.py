"""Normalize folder paths pasted from Finder, Terminal, or file URLs."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

# Straight and “smart” quote characters often copied with paths.
_QUOTE_CHARS = frozenset('"\'""''「」『』')


def _unescape_shell_path(s: str) -> str:
    """Undo shell-style escaping often pasted from Terminal or drag-and-drop."""
    s = s.replace("\\ ", " ").replace("\\~", "~")
    if "\\" in s:
        s = s.replace("\\", "")
    return s


def _resolve_existing_directory(path: Path) -> Path:
    """Return *path* if it exists; else a unique sibling match on trailing spaces."""
    if path.is_dir():
        return path
    parent = path.parent
    name = path.name
    if not name or not parent.is_dir():
        return path
    matches = [c for c in parent.iterdir() if c.is_dir() and c.name.rstrip() == name.rstrip()]
    if len(matches) == 1:
        return matches[0]
    return path


def normalize_folder_input(raw: str) -> Path:
    """Turn pasted or typed folder input into a :class:`Path`.

    Handles common macOS/iCloud paste formats:

    - Surrounding straight or smart quotes
    - ``file://`` URLs (including percent-encoded spaces)
    - Shell-style backslash escapes (``Mobile\\ Documents``, ``com\\~apple\\~CloudDocs``)
    - Leading/trailing whitespace and trailing slashes
    - Tilde expansion (``~/Library/Mobile Documents/...``)
    - A single sibling folder when the last component differs only by trailing spaces
    """
    s = raw.strip()
    while len(s) >= 2 and s[0] in _QUOTE_CHARS and s[-1] in _QUOTE_CHARS:
        s = s[1:-1].strip()
    if s.lower().startswith("file://"):
        parsed = urlparse(s)
        s = unquote(parsed.path)
    s = _unescape_shell_path(s.strip().rstrip("/"))
    path = Path(s).expanduser()
    return _resolve_existing_directory(path)


def name_key(name: str) -> str:
    """Conservative reservation key, including case/Unicode-insensitive volumes."""
    import unicodedata
    return unicodedata.normalize("NFC", name).casefold()


def available_path(destination: Path, *, restored: bool = False) -> Path:
    """Find an unused sibling; the eventual move still checks atomically."""
    import os
    if not os.path.lexists(destination):
        return destination
    stem = destination.stem + ("_restored" if restored else "")
    suffix = destination.suffix
    number = 0 if restored else 1
    while True:
        tail = "" if number == 0 else f"_{number}"
        candidate = destination.with_name(f"{stem}{tail}{suffix}")
        if not os.path.lexists(candidate):
            return candidate
        number += 1


def _rename_exclusive(source: Path, destination: Path) -> None:
    """OS atomic no-replace rename. Never emulate this with exists()+rename()."""
    import ctypes
    import errno
    import os
    import sys
    if os.name == "nt":
        os.rename(source, destination)  # Windows refuses an existing destination.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    elif hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Atomic no-replace rename unavailable")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def move_no_replace(source: Path, destination: Path, *, before_publish=None, after_publish=None) -> None:
    """Move without replacing anything, including a concurrently created target.

    Cross-device files are copied to a private sibling, flushed, and published
    atomically. The callback runs before unlinking the source on that path, so a
    recovery journal can durably identify the new copy before the old one goes.
    """
    import errno
    import os
    import shutil
    import stat
    import tempfile
    try:
        _rename_exclusive(source, destination)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.ENOTSUP, errno.ENOSYS):
            raise
        source_stat = source.lstat()
        mode = source_stat.st_mode
        if stat.S_ISDIR(mode):
            # Directory trees are never silently merged or partially copied.
            raise OSError(errno.ENOTSUP, "Cross-volume directory moves require a file-by-file plan") from exc
        temporary = None
        try:
            if stat.S_ISLNK(mode):
                os.symlink(os.readlink(source), destination)
            elif stat.S_ISREG(mode):
                fd, temporary = tempfile.mkstemp(prefix=".organizer-move-", dir=destination.parent)
                with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                current = source.lstat()
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns):
                    raise OSError(errno.EBUSY, "Source changed during cross-volume copy")
                shutil.copystat(source, temporary, follow_symlinks=False)
                if before_publish:
                    before_publish(Path(temporary))
                try:
                    _rename_exclusive(Path(temporary), destination)
                except OSError as publish_error:
                    if publish_error.errno not in (errno.ENOTSUP, errno.ENOSYS):
                        raise
                    os.link(temporary, destination)  # atomic exclusive publication
                if after_publish:
                    after_publish(destination)
                current = source.lstat()
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns, source_stat.st_ctime_ns):
                    raise OSError(errno.EBUSY, "Source changed before removing the cross-volume original; both copies retained")
                source.unlink()
                return
            else:
                raise OSError(errno.ENOTSUP, "Only regular files and symlinks can cross filesystems")
            if after_publish:
                after_publish(destination)
            current = source.lstat()
            if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (source_stat.st_dev, source_stat.st_ino, source_stat.st_mtime_ns, source_stat.st_ctime_ns):
                raise OSError(errno.EBUSY, "Source changed before unlink; both paths retained")
            source.unlink()
            return
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
    if after_publish:
        after_publish(destination)


def move_to_available(source: Path, destination: Path, *, restored: bool = False,
                      before_move=None, before_publish=None, after_publish=None) -> Path:
    """Allocate and retry only on collisions; return the actual destination."""
    candidate = available_path(destination, restored=restored)
    while True:
        if before_move:
            before_move(source, candidate)
        try:
            move_no_replace(source, candidate, before_publish=before_publish, after_publish=after_publish)
            return candidate
        except FileExistsError:
            candidate = available_path(destination, restored=restored)
