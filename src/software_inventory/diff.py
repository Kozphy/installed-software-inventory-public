"""
Snapshot comparison for fleet/personal inventory change tracking.

Consumes two JSON inventories (legacy arrays or v1.1 envelopes) and classifies
software as added, removed, changed, or unchanged for the CLI ``diff``
subcommand and ``scripts/run_inventory.ps1``.

    OLD.json + NEW.json
        → SoftwareEntry lists
        → index by identity (name + publisher + install location; **no version**)
        → classify added / removed / changed / unchanged
        → table or JSON via ``exporters.export_diff``

Why version is excluded from identity: an upgrade should read as
``1.2.3 → 1.3.0`` under Changed, not as uninstall+install noise.

Appx/MSIX rows key on package family + architecture instead, because their
install folder embeds the version (``...\\WindowsApps\\Name_1.2.3.0_x64__hash``)
and their display name is localized.

Contrast with prepare-time ``deduplication_key``, which *includes* version so
a single scan can still list two co-installed versions as separate rows.
"""


from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from software_inventory.models import SOURCE_APPX, SoftwareEntry
from software_inventory.report import load_entries_from_payload

_APPX_PATH_PREFIX = "appx:"

DIFF_COMPARE_FIELDS: tuple[str, ...] = (
    "name",
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
"""Fields compared after identity match. ``registry_path`` is omitted on purpose.

``name`` / ``publisher`` / ``install_location`` usually only appear here when
normalized identity still matches (e.g. case or trailing-slash differences).
A true publisher rename or move to a new folder changes the identity key and
shows up as removed+added instead of changed. Appx rows are the exception:
their identity ignores name and location, so a renamed or upgraded package
reports those fields here.
"""


@dataclass(frozen=True)
class FieldChange:
    """One attribute delta between matched old/new snapshot rows."""

    field: str
    old: Any
    new: Any


@dataclass(frozen=True)
class ChangedEntry:
    """
    Same logical app in both snapshots, with at least one compared field differing.

    ``old_version`` / ``new_version`` are duplicated at the top level so upgrade
    summaries stay easy to scan without digging into ``changes``.
    """

    identity: tuple[str, str, str]
    name: str
    old_version: Optional[str]
    new_version: Optional[str]
    changes: tuple[FieldChange, ...]
    old: SoftwareEntry
    new: SoftwareEntry

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize for JSON diff output (summary tooling and archives).

        Returns:
            dict[str, Any]: Name, versions, field changes, and full old/new rows.
        """
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
    """
    Headline counts for operators and automation (e.g. PowerShell runners).

    ``unchanged`` is counted but not listed in detailed export sections.
    """

    added: int
    removed: int
    changed: int
    unchanged: int

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize summary counts for JSON diff output.

        Returns:
            dict[str, Any]: added/removed/changed/unchanged integers.
        """
        return asdict(self)


@dataclass(frozen=True)
class DiffResult:
    """Complete classification of two snapshots, ready for table/JSON export."""

    added: tuple[SoftwareEntry, ...]
    removed: tuple[SoftwareEntry, ...]
    changed: tuple[ChangedEntry, ...]
    summary: DiffSummary

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the full diff for ``--format json``.

        Returns:
            dict[str, Any]: Summary plus added/removed/changed arrays.
            Unchanged rows are counted only, not enumerated.
        """
        return {
            "summary": self.summary.to_dict(),
            "added": [entry.to_dict() for entry in self.added],
            "removed": [entry.to_dict() for entry in self.removed],
            "changed": [entry.to_dict() for entry in self.changed],
        }


def identity_key(entry: SoftwareEntry) -> tuple[str, str, str]:
    """
    Match the same logical app across two snapshots.

    Version is excluded so upgrades are field changes. Publisher and install
    location *are* included: renaming the vendor string or moving the install
    folder looks like remove+add (different identity), not a soft change.

    Args:
        entry (SoftwareEntry): Inventory row to key.

    Returns:
        tuple[str, str, str]: Lowercased ``(name, publisher, install_location)``
        with trailing path separators stripped from the location.

    Notes:
        Empty/missing ``install_location`` (common) collapses many apps onto
        name+publisher only — collisions are resolved by completeness, then
        ``registry_path``. Prefer populated InstallLocation when comparing fleets.

        Appx rows return ``("appx:<family>", "", architecture)`` instead, so
        x86 and x64 builds of one package family stay distinct.
    """
    path = (entry.registry_path or "").strip().lower()
    if entry.source == SOURCE_APPX and path.startswith(_APPX_PATH_PREFIX):
        return (path, "", (entry.architecture or "").strip().lower())
    return (
        (entry.name or "").strip().lower(),
        (entry.publisher or "").strip().lower(),
        (entry.install_location or "").strip().lower().rstrip("\\/"),
    )


def _prefer_entry(current: SoftwareEntry, candidate: SoftwareEntry) -> SoftwareEntry:
    """
    Prefer the richer record when identities collide within one snapshot.

    Args:
        current (SoftwareEntry): Entry already indexed for an identity.
        candidate (SoftwareEntry): Competing entry with the same identity.

    Returns:
        SoftwareEntry: Richer entry, with registry_path as tie-breaker.
    """
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
    """
    Collapse one snapshot to a single preferred row per identity.

    Args:
        entries (Sequence[SoftwareEntry]): Rows from one JSON snapshot.

    Returns:
        dict[tuple[str, str, str], SoftwareEntry]: Identity → richest row
        (``registry_path`` tie-break). Needed because a snapshot may still
        contain near-duplicates if it was produced before prepare dedupe or
        from mixed sources.
    """
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
    """
    List ``DIFF_COMPARE_FIELDS`` that differ between two matched entries.

    Args:
        old (SoftwareEntry): Prior snapshot row.
        new (SoftwareEntry): Current snapshot row.

    Returns:
        list[FieldChange]: Ordered field deltas (may be empty).
    """
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
    """
    Classify software deltas between two snapshots for audit / fleet tracking.

    Args:
        old_entries (Sequence[SoftwareEntry]): Previous snapshot rows.
        new_entries (Sequence[SoftwareEntry]): Current snapshot rows.

    Returns:
        DiffResult: Added/removed/changed lists plus summary counts.

    Notes:
        * Within each snapshot, colliding identities keep the richer row.
        * Case-only publisher/location edits (same normalized identity) appear
          under Changed; substantive renames/moves appear as Removed+Added.
        * ``registry_path`` differences alone never mark a row Changed.
        * Unchanged matches increment the summary only (not listed in detail).
        * Output order is deterministic (sorted keys / names).
    """
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
    """
    Load software entries from a JSON inventory file.

    Supports both legacy top-level arrays and v1.1 report envelopes.
    UTF-8 with or without BOM is accepted.

    Args:
        path (Path): Path to a snapshot JSON file.

    Returns:
        list[SoftwareEntry]: Software rows from the file.

    Raises:
        ValueError: When the file cannot be read, is invalid JSON, or has an
            unsupported payload shape/schema (message includes the path).
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
    """
    Render a human-readable diff summary table.

    Args:
        result (DiffResult): Comparison output from ``compare_inventories``.

    Returns:
        str: Multi-line text with summary counts and Added/Removed/Changed
        sections (or ``(no differences)``).
    """
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
