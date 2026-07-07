#!/usr/bin/env python3
"""Bucket profiles: extension sets and classification order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

GIF_EXTS: Set[str] = {"GIF"}

VIDEO_EXTS: Set[str] = {
    "MP4", "M4V", "MOV", "AVI", "MKV", "WEBM", "WMV", "FLV", "MPEG", "MPG",
    "M2TS", "MTS", "3GP", "3G2", "OGV", "ASF", "F4V", "VOB", "DIVX",
}

IMAGE_EXTS: Set[str] = {
    "JPG", "JPEG", "JPE", "PNG", "WEBP", "BMP", "TIFF", "TIF", "HEIC", "HEIF",
    "AVIF", "JXL", "SVG", "ICO", "RAW", "CR2", "NEF", "ARW", "DNG", "ORF",
    "RW2", "PSD", "TGA", "PCX", "HDR", "EXR", "JP2", "J2K",
}

DOCUMENT_EXTS: Set[str] = {
    "PDF", "DOC", "DOCX", "DOT", "DOTX", "RTF", "TXT", "MD", "ODT", "ODS",
    "ODP", "XLS", "XLSX", "PPT", "PPTX", "CSV", "EPUB", "MOBI",
}

AUDIO_EXTS: Set[str] = {
    "MP3", "M4A", "AAC", "FLAC", "WAV", "OGG", "OPUS", "WMA", "AIFF", "APE",
    "MID", "MIDI",
}

ARCHIVE_EXTS: Set[str] = {
    "ZIP", "RAR", "7Z", "TAR", "GZ", "BZ2", "XZ", "ISO", "DMG", "PKG", "CAB",
}

CODE_EXTS: Set[str] = {
    "PY", "JS", "TS", "TSX", "JSX", "JAVA", "GO", "RS", "RB", "PHP", "C",
    "CPP", "H", "HPP", "CS", "SWIFT", "KT", "SQL", "SH", "JSON", "YAML",
    "YML", "TOML", "XML", "HTML", "CSS", "SCSS", "R", "LUA", "VUE",
}

STANDARD_BUCKETS: List[Tuple[str, Set[str]]] = [
    ("GIFs", GIF_EXTS),
    ("Videos", VIDEO_EXTS),
    ("Images", IMAGE_EXTS),
]

EXTENDED_EXTRA_BUCKETS: List[Tuple[str, Set[str]]] = [
    ("Documents", DOCUMENT_EXTS),
    ("Audio", AUDIO_EXTS),
    ("Archives", ARCHIVE_EXTS),
    ("Code", CODE_EXTS),
]

PROFILE_STANDARD = "standard"
PROFILE_EXTENDED = "extended"


def buckets_for_profile(name: str) -> List[Tuple[str, Set[str]]]:
    if name == PROFILE_EXTENDED:
        return STANDARD_BUCKETS + EXTENDED_EXTRA_BUCKETS
    return list(STANDARD_BUCKETS)


def bucket_names_for_profile(name: str) -> List[str]:
    return [b[0] for b in buckets_for_profile(name)] + ["Other"]


def canonical_dir_map(profile: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for bucket, _ in buckets_for_profile(profile):
        out[bucket.casefold()] = bucket
    out["other"] = "Other"
    return out


def extension_bucket_map(buckets: List[Tuple[str, Set[str]]]) -> Dict[str, str]:
    """Flatten an ordered bucket list into ext -> bucket (first bucket wins on overlap)."""
    out: Dict[str, str] = {}
    for bucket, exts in buckets:
        for ext in exts:
            out.setdefault(ext, bucket)
    return out


def bucket_for_extension(ext_upper: str, profile: str, custom_buckets: Optional[List[Tuple[str, Set[str]]]] = None) -> str:
    buckets = custom_buckets if custom_buckets is not None else buckets_for_profile(profile)
    for bucket, exts in buckets:
        if ext_upper in exts:
            return bucket
    return "Other"


def bucket_for_filename(
    file_name: str,
    profile: str,
    custom_buckets: Optional[List[Tuple[str, Set[str]]]] = None,
) -> str:
    suffix = Path(file_name).suffix
    ext = suffix[1:].upper() if suffix else ""
    return bucket_for_extension(ext, profile, custom_buckets)


def load_profile_from_file(path: Path) -> List[Tuple[str, Set[str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Profile file must be a JSON object")
    ordered: List[Tuple[str, Set[str]]] = []
    for name, raw in data.items():
        if name.casefold() == "other":
            continue
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"Bucket {name!r} must be a list of extensions")
        exts = {str(x).lstrip(".").upper() for x in raw if str(x).strip()}
        ordered.append((str(name), exts))
    if not ordered:
        raise ValueError("Profile file must define at least one bucket (Other is implicit)")
    return ordered


def resolve_profile(profile_arg: str) -> Tuple[str, List[Tuple[str, Set[str]]]]:
    p = profile_arg.strip()
    if p == PROFILE_EXTENDED:
        return PROFILE_EXTENDED, buckets_for_profile(PROFILE_EXTENDED)
    if p in ("", PROFILE_STANDARD):
        return PROFILE_STANDARD, buckets_for_profile(PROFILE_STANDARD)
    path = Path(p).expanduser()
    if path.is_file():
        return f"custom:{path.name}", load_profile_from_file(path)
    raise ValueError(f"Unknown profile {profile_arg!r}; use standard, extended, or a JSON file path")
