#!/usr/bin/env python3
"""Lightweight content sniffing for extensionless files (stdlib only)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "Images"),
    (b"\xff\xd8\xff", "Images"),
    (b"GIF87a", "GIFs"),
    (b"GIF89a", "GIFs"),
    (b"%PDF", "Documents"),
    (b"PK\x03\x04", "Archives"),
    (b"\x1f\x8b\x08", "Archives"),
    (b"ID3", "Audio"),
    (b"fLaC", "Audio"),
    (b"OggS", "Audio"),
]


def sniff_bucket_from_file(path: Path, *, max_read: int = 16) -> Optional[str]:
    try:
        with path.open("rb") as f:
            head = f.read(max_read)
    except OSError:
        return None
    if not head:
        return None
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "Images"
    for sig, bucket in _SIGNATURES:
        if head.startswith(sig):
            return bucket
    return None
