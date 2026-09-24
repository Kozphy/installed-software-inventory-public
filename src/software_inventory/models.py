"""
Canonical data model for one installed application discovered on a Windows PC.

``SoftwareEntry`` is the shared currency of the inventory tool: collectors emit
it, prepare/dedupe refine it, exporters serialize it, and ``diff`` compares
snapshots of it. Field names intentionally mirror Uninstall Registry values so
auditors can trace a row back to its hive key via ``registry_path``.

Rows come from two read-only collectors, tagged by ``source``: the Uninstall
Registry (``registry``) and the per-user Appx/MSIX package catalog (``appx``,
Microsoft Store and sideloaded packages). Portable apps are still invisible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Optional

SOURCE_REGISTRY = "registry"
SOURCE_APPX = "appx"
KNOWN_SOURCES: tuple[str, ...] = (SOURCE_REGISTRY, SOURCE_APPX)

_PROVENANCE_FIELDS = frozenset(
    {"name", "registry_path", "scope", "architecture", "system_component", "source"}
)


@dataclass(frozen=True)
class SoftwareEntry:
    """
    Immutable inventory row for one installed application (Uninstall key or
    Appx package).

    Built for personal audits and fleet snapshot diffs: it carries enough
    provenance to explain *where* a row came from, while staying safe to
    serialize (uninstall strings are reported, never executed).

    Attributes:
        name: Display name from ``DisplayName``. Blank names are dropped before
            export; JSON reload also rejects missing/blank names.
        version: Display version string (not semver-normalized).
        publisher: Vendor string when present.
        install_date: Normalized ``YYYY-MM-DD``, or None if missing/invalid.
        install_location: Install folder when present; empty/missing locations
            weaken ``diff`` identity (many apps share blank location).
        estimated_size_kb: Registry ``EstimatedSize`` in KB. Often approximate
            or absent; never converted to bytes so consumers keep Registry units.
        scope: Usually ``machine`` (HKLM) or ``current_user`` (HKCU). May be
            ``unknown`` when rehydrated from incomplete JSON.
        architecture: ``64-bit``, ``32-bit``, or ``unknown`` (HKCU / incomplete
            JSON). Labels the Registry *view*, not the binary's PE machine type.
        uninstall_string: Uninstall command text for documentation only.
        quiet_uninstall_string: Quiet uninstall command when publishers provide one.
        registry_path: Full Uninstall subkey path used as the source of truth.
            Appx rows use ``appx:<PackageFamilyName>``, which is stable across
            package versions.
        release_type: Registry ``ReleaseType``; feeds update/hotfix heuristics.
        system_component: True when Registry ``SystemComponent`` is set (or the
            Appx package is a framework / OS-signed); hidden from default
            exports to reduce OS plumbing noise.
        source: Collector that produced the row: ``registry`` or ``appx``.
            Snapshots written before v1.3 have no tag and load as ``registry``.
    """

    name: str
    version: Optional[str]
    publisher: Optional[str]
    install_date: Optional[str]
    install_location: Optional[str]
    estimated_size_kb: Optional[int]
    scope: str
    architecture: str
    uninstall_string: Optional[str]
    quiet_uninstall_string: Optional[str]
    registry_path: str
    release_type: Optional[str]
    system_component: bool
    source: str = "registry"

    def to_dict(self) -> dict[str, Any]:
        """
        Flatten this entry for JSON/CSV and for ``diff`` serialization.

        Returns:
            dict[str, Any]: One key per dataclass field, JSON-friendly values.
        """
        return asdict(self)

    def completeness_score(self) -> int:
        """
        Rank metadata richness so overlapping Registry views keep the better row.

        Used by both prepare-time deduplication and within-snapshot ``diff``
        identity collisions. A 64-bit view row with publisher/size should beat a
        sparse duplicate from a parallel 32-bit view.

        Returns:
            int: Count of optional fields that are present and non-blank.

        Notes:
            ``name``, ``registry_path``, ``scope``, ``architecture``,
            ``system_component``, and ``source`` are excluded so provenance
            alone cannot inflate the score. Ties fall back to lexicographic
            ``registry_path``.
        """
        score = 0
        for field in fields(self):
            if field.name in _PROVENANCE_FIELDS:
                continue
            value = getattr(self, field.name)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            score += 1
        return score


CSV_FIELDNAMES: tuple[str, ...] = (
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
    "registry_path",
    "release_type",
    "system_component",
    "source",
)
"""Public CSV column contract; order is stable across releases and must match SoftwareEntry.

New columns are only ever appended so positional CSV consumers keep working.
"""
