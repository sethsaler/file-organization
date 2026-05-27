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
