#!/usr/bin/env python3
"""Deterministic routing rules and the Downloader-to-Archive recipe.

Rules are deliberately simple and explainable: every destination is selected by
the first enabled matching rule, and every rule match is included in previews.
Relative rule destinations are always confined to the organizer root.  The
archive recipe is the only built-in path that can move to another root, and it
requires that root explicitly.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


RULES_VERSION = 1
NEEDS_REVIEW_DIR_NAME = "Needs Review"
FALLBACK_BUCKET = "bucket"
FALLBACK_NEEDS_REVIEW = "needs-review"
FALLBACK_LEAVE = "leave"
VALID_FALLBACKS = frozenset({FALLBACK_BUCKET, FALLBACK_NEEDS_REVIEW, FALLBACK_LEAVE})


def _string_list(value: Any, *, field_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a string or JSON array")
    return [str(item).strip() for item in value if str(item).strip()]


def _matches_any(value: str, patterns: Sequence[str]) -> bool:
    folded = value.casefold()
    return any(
        fnmatch.fnmatch(folded, pattern.casefold())
        or fnmatch.fnmatchcase(folded, pattern.casefold())
        for pattern in patterns
    )


def _safe_component(value: str, fallback: str = "Root") -> str:
    """Return one filesystem component without allowing path traversal."""
    cleaned = value.strip().replace("/", "-").replace(os.sep, "-")
    if os.altsep:
        cleaned = cleaned.replace(os.altsep, "-")
    if cleaned in {"", ".", ".."}:
        return fallback
    return cleaned


def _validate_relative_destination(value: str, *, field_name: str = "destination") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    candidate = Path(raw)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"{field_name} must stay inside its configured root")
    return raw


@dataclass(frozen=True)
class RuleContext:
    base: Path
    source: Path
    bucket: str
    relative_path: str
    parent_name: str
    top_parent: str
    extension: str
    size_bytes: int
    modified_at: datetime
    source_url: str = ""


@dataclass(frozen=True)
class RouteDecision:
    destination: Optional[Path]
    category: str
    reason: str
    rule_id: Optional[str] = None
    external: bool = False


@dataclass
class RoutingRule:
    id: str
    name: str
    destination: str
    enabled: bool = True
    extensions: List[str] = field(default_factory=list)
    filename_globs: List[str] = field(default_factory=list)
    path_globs: List[str] = field(default_factory=list)
    parent_globs: List[str] = field(default_factory=list)
    source_url_contains: List[str] = field(default_factory=list)
    min_size_bytes: Optional[int] = None
    max_size_bytes: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], index: int) -> "RoutingRule":
        if not isinstance(data, Mapping):
            raise ValueError(f"rules[{index}] must be a JSON object")
        match = data.get("match") or {}
        if not isinstance(match, Mapping):
            raise ValueError(f"rules[{index}].match must be a JSON object")
        rule_id = str(data.get("id") or f"rule-{index + 1}").strip()
        name = str(data.get("name") or rule_id).strip()
        destination = _validate_relative_destination(
            str(data.get("destination") or ""),
            field_name=f"rules[{index}].destination",
        )
        min_size = match.get("min_size_bytes")
        max_size = match.get("max_size_bytes")
        rule = cls(
            id=rule_id,
            name=name,
            destination=destination,
            enabled=bool(data.get("enabled", True)),
            extensions=[x.lstrip(".").casefold() for x in _string_list(match.get("extensions"), field_name="extensions")],
            filename_globs=_string_list(match.get("filename_globs") or match.get("filename"), field_name="filename_globs"),
            path_globs=_string_list(match.get("path_globs") or match.get("path"), field_name="path_globs"),
            parent_globs=_string_list(match.get("parent_globs") or match.get("parent"), field_name="parent_globs"),
            source_url_contains=[x.casefold() for x in _string_list(match.get("source_url_contains"), field_name="source_url_contains")],
            min_size_bytes=int(min_size) if min_size is not None else None,
            max_size_bytes=int(max_size) if max_size is not None else None,
        )
        if rule.min_size_bytes is not None and rule.min_size_bytes < 0:
            raise ValueError(f"rules[{index}].match.min_size_bytes must be non-negative")
        if rule.max_size_bytes is not None and rule.max_size_bytes < 0:
            raise ValueError(f"rules[{index}].match.max_size_bytes must be non-negative")
        if (
            rule.min_size_bytes is not None
            and rule.max_size_bytes is not None
            and rule.min_size_bytes > rule.max_size_bytes
        ):
            raise ValueError(f"rules[{index}] has min_size_bytes greater than max_size_bytes")
        if not rule.has_matchers:
            raise ValueError(f"rules[{index}] has no match conditions")
        return rule

    @property
    def has_matchers(self) -> bool:
        return bool(
            self.extensions
            or self.filename_globs
            or self.path_globs
            or self.parent_globs
            or self.source_url_contains
            or self.min_size_bytes is not None
            or self.max_size_bytes is not None
        )

    def matches(self, context: RuleContext) -> bool:
        if not self.enabled:
            return False
        if self.extensions and context.extension.casefold() not in self.extensions:
            return False
        if self.filename_globs and not _matches_any(context.source.name, self.filename_globs):
            return False
        if self.path_globs and not _matches_any(context.relative_path, self.path_globs):
            return False
        if self.parent_globs and not _matches_any(context.parent_name, self.parent_globs):
            return False
        if self.source_url_contains:
            source_folded = context.source_url.casefold()
            if not any(fragment in source_folded for fragment in self.source_url_contains):
                return False
        if self.min_size_bytes is not None and context.size_bytes < self.min_size_bytes:
            return False
        if self.max_size_bytes is not None and context.size_bytes > self.max_size_bytes:
            return False
        return True

    def render_destination(self, context: RuleContext) -> Path:
        values = {
            "bucket": _safe_component(context.bucket),
            "parent": _safe_component(context.parent_name),
            "top_parent": _safe_component(context.top_parent),
            "year": f"{context.modified_at.year:04d}",
            "month": f"{context.modified_at.month:02d}",
        }
        try:
            rendered = self.destination.format_map(values)
        except KeyError as exc:
            raise ValueError(f"Rule {self.name!r} uses unknown destination placeholder {exc}") from exc
        relative = Path(_validate_relative_destination(rendered))
        destination = (context.base / relative).resolve()
        try:
            destination.relative_to(context.base.resolve())
        except ValueError as exc:
            raise ValueError(f"Rule {self.name!r} destination escapes the organizer root") from exc
        return destination

    def to_dict(self) -> Dict[str, Any]:
        match: Dict[str, Any] = {}
        if self.extensions:
            match["extensions"] = self.extensions
        if self.filename_globs:
            match["filename_globs"] = self.filename_globs
        if self.path_globs:
            match["path_globs"] = self.path_globs
        if self.parent_globs:
            match["parent_globs"] = self.parent_globs
        if self.source_url_contains:
            match["source_url_contains"] = self.source_url_contains
        if self.min_size_bytes is not None:
            match["min_size_bytes"] = self.min_size_bytes
        if self.max_size_bytes is not None:
            match["max_size_bytes"] = self.max_size_bytes
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "match": match,
            "destination": self.destination,
        }


@dataclass
class RuleSet:
    name: str = "Organization rules"
    unmatched: str = FALLBACK_BUCKET
    rules: List[RoutingRule] = field(default_factory=list)
    source_path: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source_path: Optional[Path] = None) -> "RuleSet":
        if not isinstance(data, Mapping):
            raise ValueError("Rules file must contain a JSON object")
        version = int(data.get("version", RULES_VERSION))
        if version != RULES_VERSION:
            raise ValueError(f"Unsupported rules version: {version}")
        unmatched = str(data.get("unmatched", FALLBACK_BUCKET)).strip().casefold()
        if unmatched not in VALID_FALLBACKS:
            raise ValueError(f"unmatched must be one of: {', '.join(sorted(VALID_FALLBACKS))}")
        raw_rules = data.get("rules") or []
        if not isinstance(raw_rules, list):
            raise ValueError("rules must be a JSON array")
        return cls(
            name=str(data.get("name") or "Organization rules").strip(),
            unmatched=unmatched,
            rules=[RoutingRule.from_dict(item, index) for index, item in enumerate(raw_rules)],
            source_path=source_path,
        )

    def decide(self, context: RuleContext) -> Optional[RouteDecision]:
        for rule in self.rules:
            if rule.matches(context):
                return RouteDecision(
                    destination=rule.render_destination(context),
                    category=rule.name,
                    reason=f"Rule: {rule.name}",
                    rule_id=rule.id,
                )
        if self.unmatched == FALLBACK_NEEDS_REVIEW:
            return RouteDecision(
                destination=context.base / NEEDS_REVIEW_DIR_NAME,
                category=NEEDS_REVIEW_DIR_NAME,
                reason="No rule matched — held for review",
            )
        if self.unmatched == FALLBACK_LEAVE:
            return RouteDecision(
                destination=None,
                category="Unmatched",
                reason="No rule matched — left in place",
            )
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": RULES_VERSION,
            "name": self.name,
            "unmatched": self.unmatched,
            "rules": [rule.to_dict() for rule in self.rules],
        }


def load_rule_set(path: Path) -> RuleSet:
    source = path.expanduser().resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read rules file: {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid rules JSON in {source}: {exc}") from exc
    return RuleSet.from_dict(data, source_path=source)


def save_rule_set(path: Path, rule_set: RuleSet) -> Path:
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(json.dumps(rule_set.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(destination)
    return destination


def starter_rule_set() -> RuleSet:
    return RuleSet(
        name="Starter rules",
        unmatched=FALLBACK_NEEDS_REVIEW,
        rules=[
            RoutingRule(
                id="receipts",
                name="Receipts",
                destination="Documents/Receipts",
                extensions=["pdf"],
                filename_globs=["*receipt*", "*invoice*"],
            ),
            RoutingRule(
                id="screenshots",
                name="Screenshots",
                destination="Images/Screenshots/{year}/{month}",
                extensions=["png"],
                filename_globs=["Screenshot*", "Screen Shot*"],
            ),
        ],
    )


def append_rule_for_review_choice(
    path: Path,
    *,
    source: Path,
    destination: str,
    criterion: str,
) -> RoutingRule:
    """Append one deterministic rule learned from a reviewed file."""
    try:
        rule_set = load_rule_set(path)
    except ValueError:
        if path.exists():
            raise
        rule_set = starter_rule_set()
        rule_set.rules = []

    criterion_key = criterion.strip().casefold()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    kwargs: Dict[str, Any] = {}
    label = source.suffix.lstrip(".").upper() or source.name
    if criterion_key == "extension":
        ext = source.suffix.lstrip(".").casefold()
        if not ext:
            raise ValueError("This file has no extension; choose Filename or Parent folder")
        kwargs["extensions"] = [ext]
    elif criterion_key == "filename":
        kwargs["filename_globs"] = [source.name]
    elif criterion_key in {"parent", "parent folder"}:
        kwargs["parent_globs"] = [source.parent.name]
        label = source.parent.name
    else:
        raise ValueError("criterion must be Extension, Filename, or Parent folder")
    rule = RoutingRule(
        id=f"learned-{stamp}",
        name=f"Reviewed {label}",
        destination=_validate_relative_destination(destination),
        **kwargs,
    )
    rule_set.rules.append(rule)
    save_rule_set(path, rule_set)
    return rule


def build_rule_context(
    *,
    base: Path,
    source: Path,
    bucket: str,
    source_url: str = "",
) -> RuleContext:
    try:
        relative = source.relative_to(base)
    except ValueError:
        relative = Path(source.name)
    try:
        stat = source.stat()
        size = int(stat.st_size)
        modified = datetime.fromtimestamp(stat.st_mtime)
    except OSError:
        size = 0
        modified = datetime.now()
    parts = relative.parts
    top_parent = parts[0] if len(parts) > 1 else "Root"
    parent = source.parent.name if source.parent != base else "Root"
    return RuleContext(
        base=base,
        source=source,
        bucket=bucket,
        relative_path=relative.as_posix(),
        parent_name=parent,
        top_parent=top_parent,
        extension=source.suffix.lstrip(".").casefold(),
        size_bytes=size,
        modified_at=modified,
        source_url=source_url,
    )


@dataclass
class ArchiveRecipe:
    archive_root: Path
    folder_mappings: Dict[str, str] = field(default_factory=dict)
    manual_library_dir: str = "Manual Library"
    recents_dir: str = "Recents"
    needs_review_dir: str = NEEDS_REVIEW_DIR_NAME

    def __post_init__(self) -> None:
        self.archive_root = self.archive_root.expanduser().resolve()
        self.manual_library_dir = _validate_relative_destination(self.manual_library_dir, field_name="manual_library_dir")
        self.recents_dir = _validate_relative_destination(self.recents_dir, field_name="recents_dir")
        self.needs_review_dir = _validate_relative_destination(self.needs_review_dir, field_name="needs_review_dir")
        normalized: Dict[str, str] = {}
        for source, destination in self.folder_mappings.items():
            key = str(source).strip().casefold()
            if not key:
                continue
            normalized[key] = _validate_relative_destination(str(destination), field_name=f"mapping for {source!r}")
        self.folder_mappings = normalized

    def validate_source_root(self, source_root: Path) -> None:
        source = source_root.expanduser().resolve()
        if (
            self.archive_root == source
            or source in self.archive_root.parents
            or self.archive_root in source.parents
        ):
            raise ValueError("Archive root and Downloader source must not overlap")

    def _destination(self, relative: Path) -> Path:
        destination = (self.archive_root / relative).resolve()
        if destination != self.archive_root and self.archive_root not in destination.parents:
            raise ValueError("Archive destination escapes the configured Archive root")
        return destination

    def decide(self, *, source_root: Path, source: Path, bucket: str) -> RouteDecision:
        self.validate_source_root(source_root)
        relative = source.relative_to(source_root)
        if len(relative.parts) == 1:
            if bucket == "Other":
                destination = self._destination(Path(self.needs_review_dir))
                category = NEEDS_REVIEW_DIR_NAME
                reason = "Archive recipe: loose unclassified file held for review"
            else:
                destination = self._destination(Path(self.recents_dir) / bucket)
                category = f"{self.recents_dir}/{bucket}"
                reason = f"Archive recipe: loose {bucket.lower()} to {self.recents_dir}/{bucket}"
            return RouteDecision(destination, category, reason, external=True)

        source_folder = relative.parts[0]
        mapped = self.folder_mappings.get(source_folder.casefold())
        if mapped:
            destination = self._destination(Path(mapped))
            return RouteDecision(
                destination,
                mapped,
                f"Archive recipe: mapped {source_folder} to {mapped}",
                external=True,
            )

        destination = self._destination(Path(self.needs_review_dir) / _safe_component(source_folder))
        return RouteDecision(
            destination,
            NEEDS_REVIEW_DIR_NAME,
            f"Archive recipe: unknown folder {source_folder} held for review",
            external=True,
        )


def load_archive_recipe(archive_root: Path, mapping_file: Optional[Path] = None) -> ArchiveRecipe:
    mappings: Dict[str, str] = {}
    options: Dict[str, str] = {}
    if mapping_file:
        source = mapping_file.expanduser().resolve()
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Could not read archive mapping: {source}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid archive mapping JSON in {source}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ValueError("Archive mapping must contain a JSON object")
        raw_mappings = data.get("mappings", data)
        if not isinstance(raw_mappings, Mapping):
            raise ValueError("Archive mapping 'mappings' must be a JSON object")
        metadata_keys = {"version", "manual_library_dir", "recents_dir", "needs_review_dir", "mappings"}
        mappings = {str(k): str(v) for k, v in raw_mappings.items() if k not in metadata_keys}
        for key in ("manual_library_dir", "recents_dir", "needs_review_dir"):
            if key in data:
                options[key] = str(data[key])
    return ArchiveRecipe(archive_root=archive_root, folder_mappings=mappings, **options)


def archive_mapping_template() -> Dict[str, Any]:
    return {
        "version": 1,
        "manual_library_dir": "Manual Library",
        "recents_dir": "Recents",
        "needs_review_dir": NEEDS_REVIEW_DIR_NAME,
        "mappings": {
            "example-creator-folder": "Manual Library/Example Creator"
        },
    }
