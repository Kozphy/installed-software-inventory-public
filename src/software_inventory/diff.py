"""Compare two inventory snapshots and report added/removed/changed software."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from software_inventory.models import SoftwareEntry
from software_inventory.report import load_entries_from_payload


DIFF_COMPARE_FIELDS: tuple[str, ...] = (
    "version",
    "publisher",
    "install_date",
    "install_location",
    "estimated_size_kb",
    "scope",
    "architecture",
    "uninstall_string",
    "quiet_uninstall_string",
    "release_type",
    "system_component",
)


@dataclass(frozen=True)
class FieldChange:
    """A single field that differs between two inventory snapshots."""

    field: str
    old: Any
    new: Any


@dataclass(frozen=True)
class ChangedEntry:
    """An application present in both snapshots with one or more field changes."""

    identity: tuple[str, str, str]
    name: str
    old_version: Optional[str]
    new_version: Optional[str]
    changes: tuple[FieldChange, ...]
    old: SoftwareEntry
    new: SoftwareEntry

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "name": self.name,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "changes": [asdict(change) for change in self.changes],
            "old": self.old.to_dict(),
            "new": self.new.to_dict(),
        }


@dataclass(frozen=True)
class DiffSummary:
    """Aggregate counts for a snapshot comparison."""

    added: int
    removed: int
    changed: int
    unchanged: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass(frozen=True)
class DiffResult:
    """Full result of comparing two inventory snapshots."""

    added: tuple[SoftwareEntry, ...]
    removed: tuple[SoftwareEntry, ...]
    changed: tuple[ChangedEntry, ...]
    summary: DiffSummary

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "summary": self.summary.to_dict(),
            "added": [entry.to_dict() for entry in self.added],
            "removed": [entry.to_dict() for entry in self.removed],
            "changed": [entry.to_dict() for entry in self.changed],
        }


def identity_key(entry: SoftwareEntry) -> tuple[str, str, str]:
    """Build a stable identity from name, publisher, and install location.

    Version is intentionally excluded so upgrades appear as changes.
    """
    return (
        (entry.name or "").strip().lower(),
        (entry.publisher or "").strip().lower(),
        (entry.install_location or "").strip().lower().rstrip("\\/"),
    )


def _prefer_entry(current: SoftwareEntry, candidate: SoftwareEntry) -> SoftwareEntry:
    """Prefer the richer record when identities collide within one snapshot."""
    if candidate.completeness_score() > current.completeness_score():
        return candidate
    if candidate.completeness_score() < current.completeness_score():
        return current
    if candidate.registry_path < current.registry_path:
        return candidate
    return current


def index_by_identity(
    entries: Sequence[SoftwareEntry],
) -> dict[tuple[str, str, str], SoftwareEntry]:
    """Index entries by identity, resolving collisions deterministically."""
    indexed: dict[tuple[str, str, str], SoftwareEntry] = {}
    for entry in entries:
        key = identity_key(entry)
        existing = indexed.get(key)
        if existing is None:
            indexed[key] = entry
        else:
            indexed[key] = _prefer_entry(existing, entry)
    return indexed


def _field_changes(old: SoftwareEntry, new: SoftwareEntry) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for name in DIFF_COMPARE_FIELDS:
        old_value = getattr(old, name)
        new_value = getattr(new, name)
        if old_value != new_value:
            changes.append(FieldChange(field=name, old=old_value, new=new_value))
    return changes


def compare_inventories(
    old_entries: Sequence[SoftwareEntry],
    new_entries: Sequence[SoftwareEntry],
) -> DiffResult:
    """Compare two inventories and return added, removed, and changed entries."""
    old_index = index_by_identity(old_entries)
    new_index = index_by_identity(new_entries)

    added: list[SoftwareEntry] = []
    removed: list[SoftwareEntry] = []
    changed: list[ChangedEntry] = []
    unchanged = 0

    for key in sorted(set(old_index) | set(new_index)):
        old = old_index.get(key)
        new = new_index.get(key)
        if old is None and new is not None:
            added.append(new)
        elif new is None and old is not None:
            removed.append(old)
        elif old is not None and new is not None:
            changes = _field_changes(old, new)
            if changes:
                changed.append(
                    ChangedEntry(
                        identity=key,
                        name=new.name or old.name,
                        old_version=old.version,
                        new_version=new.version,
                        changes=tuple(changes),
                        old=old,
                        new=new,
                    )
                )
            else:
                unchanged += 1

    added.sort(key=lambda e: ((e.name or "").lower(), e.registry_path))
    removed.sort(key=lambda e: ((e.name or "").lower(), e.registry_path))
    changed.sort(key=lambda e: ((e.name or "").lower(), e.identity))

    summary = DiffSummary(
        added=len(added),
        removed=len(removed),
        changed=len(changed),
        unchanged=unchanged,
    )
    return DiffResult(
        added=tuple(added),
        removed=tuple(removed),
        changed=tuple(changed),
        summary=summary,
    )


def load_inventory_file(path: Path) -> list[SoftwareEntry]:
    """Load software entries from a JSON inventory file.

    Supports both legacy top-level arrays and v1.1 report envelopes.
    UTF-8 with or without BOM is accepted.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc

    try:
        return load_entries_from_payload(payload)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def format_diff_table(result: DiffResult) -> str:
    """Render a human-readable diff summary table."""
    lines: list[str] = [
        "Inventory Diff Summary",
        "======================",
        f"Added:      {result.summary.added}",
        f"Removed:    {result.summary.removed}",
        f"Changed:    {result.summary.changed}",
        f"Unchanged:  {result.summary.unchanged}",
        "",
    ]

    if result.added:
        lines.append("Added")
        lines.append("-----")
        for entry in result.added:
            version = entry.version or "?"
            publisher = entry.publisher or "?"
            lines.append(f"  + {entry.name} ({version}) — {publisher}")
        lines.append("")

    if result.removed:
        lines.append("Removed")
        lines.append("-------")
        for entry in result.removed:
            version = entry.version or "?"
            publisher = entry.publisher or "?"
            lines.append(f"  - {entry.name} ({version}) — {publisher}")
        lines.append("")

    if result.changed:
        lines.append("Changed")
        lines.append("-------")
        for item in result.changed:
            old_v = item.old_version or "?"
            new_v = item.new_version or "?"
            lines.append(f"  ~ {item.name}: {old_v} → {new_v}")
            for change in item.changes:
                lines.append(f"      {change.field}: {change.old!r} → {change.new!r}")
        lines.append("")

    if not (result.added or result.removed or result.changed):
        lines.append("(no differences)")

    return "\n".join(lines).rstrip() + "\n"
