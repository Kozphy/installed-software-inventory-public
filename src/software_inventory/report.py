"""
Versioned inventory report envelope and scan metadata.

Builds the versioned JSON report shape used as the default CLI
``--format json`` output and as the preferred snapshot format for ``diff``:

    prepared SoftwareEntry list + PrepareStats
        → ScanMetadata (timing, host, filter counts)
        → InventoryReport envelope
        → exporters.export_json / load_entries_from_payload

Also deserializes legacy top-level arrays and v1.1/v1.2 envelopes so older
snapshots remain comparable. v1.2 only adds the per-row ``source`` tag; v1.1
rows load with ``source="registry"``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from software_inventory.models import SOURCE_REGISTRY, SoftwareEntry

SCHEMA_VERSION = "1.2"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.1", "1.2"})


@dataclass(frozen=True)
class ScanMetadata:
    """
    Audit block embedded in every v1.1 JSON snapshot.

    Answers: when did we scan, on which host/platform, which sources ran, and
    how many rows survived dedupe/filters — without recording Windows usernames
    or other account identifiers. Hostname is kept so fleet files can be told
    apart; redact downstream if that is too identifying.
    """

    started_at: str
    completed_at: str
    duration_ms: int
    hostname: str
    platform: str
    collector_sources: tuple[str, ...]
    raw_entry_count: int
    deduplicated_count: int
    filtered_system_component_count: int
    filtered_update_count: int
    result_count: int

    def to_dict(self) -> dict[str, Any]:
        """
        Convert scan metadata into a JSON-serializable dictionary.

        Returns:
            dict[str, Any]: Metadata with ``collector_sources`` as a list.
        """
        data = asdict(self)
        data["collector_sources"] = list(self.collector_sources)
        return data


@dataclass(frozen=True)
class InventoryReport:
    """
    Versioned snapshot document (``schema_version`` + ``scan`` + ``software``).

    Default JSON export shape since v1.1. Consumers that still expect a bare
    array should use ``--legacy-json`` or read ``payload['software']``.
    """

    schema_version: str
    scan: ScanMetadata
    software: tuple[SoftwareEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the report into a JSON-serializable envelope.

        Returns:
            dict[str, Any]: Object with ``schema_version``, ``scan``, ``software``.
        """
        return {
            "schema_version": self.schema_version,
            "scan": self.scan.to_dict(),
            "software": [entry.to_dict() for entry in self.software],
        }


@dataclass
class PrepareStats:
    """
    Counts collected while preparing inventory results.

    Responsibilities:
        * Track raw/dedup/filter/result sizes for ScanMetadata.
        * Expose ``deduplicated_count`` as raw − after-dedup for reports.
    """

    raw_entry_count: int = 0
    after_dedup_count: int = 0
    filtered_system_component_count: int = 0
    filtered_update_count: int = 0
    result_count: int = 0

    @property
    def deduplicated_count(self) -> int:
        """
        Number of entries removed by deduplication.

        Returns:
            int: Non-negative count of duplicates dropped.
        """
        return max(0, self.raw_entry_count - self.after_dedup_count)


def utc_now_iso() -> str:
    """
    Return the current UTC time as an ISO-8601 string with a ``Z`` suffix.

    Returns:
        str: Second-precision UTC timestamp (e.g. ``2026-07-17T06:00:00Z``).
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def duration_ms(started_at: datetime, completed_at: datetime) -> int:
    """
    Compute elapsed milliseconds between two aware datetimes.

    Args:
        started_at (datetime): Scan start (UTC-aware preferred).
        completed_at (datetime): Scan end.

    Returns:
        int: Elapsed milliseconds, floored at 0.
    """
    delta = completed_at - started_at
    return max(0, int(delta.total_seconds() * 1000))


def build_scan_metadata(
    *,
    started_at: datetime,
    completed_at: datetime,
    hostname: str,
    platform: str,
    collector_sources: Sequence[str],
    stats: PrepareStats,
) -> ScanMetadata:
    """
    Build scan metadata from timing and prepare statistics.

    Args:
        started_at (datetime): When collection began.
        completed_at (datetime): When prepare/filter finished.
        hostname (str): Machine hostname for fleet snapshot distinction.
        platform (str): ``platform.platform()`` (or test override) string.
        collector_sources (Sequence[str]): Human-readable source labels.
        stats (PrepareStats): Counts from ``prepare_inventory_with_stats``.

    Returns:
        ScanMetadata: UTC-normalized metadata ready for the report envelope.
    """
    return ScanMetadata(
        started_at=started_at.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        completed_at=completed_at.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        duration_ms=duration_ms(started_at, completed_at),
        hostname=hostname,
        platform=platform,
        collector_sources=tuple(collector_sources),
        raw_entry_count=stats.raw_entry_count,
        deduplicated_count=stats.deduplicated_count,
        filtered_system_component_count=stats.filtered_system_component_count,
        filtered_update_count=stats.filtered_update_count,
        result_count=stats.result_count,
    )


def build_report(
    entries: Sequence[SoftwareEntry],
    scan: ScanMetadata,
    *,
    schema_version: str = SCHEMA_VERSION,
) -> InventoryReport:
    """
    Assemble a versioned inventory report.

    Args:
        entries (Sequence[SoftwareEntry]): Prepared software rows.
        scan (ScanMetadata): Audit metadata for this run.
        schema_version (str): Envelope version (default ``SCHEMA_VERSION``).

    Returns:
        InventoryReport: Immutable report ready for JSON export.
    """
    return InventoryReport(
        schema_version=schema_version,
        scan=scan,
        software=tuple(entries),
    )


def entry_from_dict(data: dict[str, Any]) -> SoftwareEntry:
    """
    Rehydrate one software object from a snapshot JSON dict.

    Tolerates partial rows (missing optionals → None/False) so older or
    hand-edited files still load. Missing/blank ``name`` is hard-failed.

    Args:
        data (dict[str, Any]): One element of a legacy array or ``software`` list.

    Returns:
        SoftwareEntry: Row ready for diff/export. Missing ``scope`` /
        ``architecture`` become ``unknown``; missing ``registry_path`` becomes
        ``""``; missing ``source`` becomes ``registry``.

    Raises:
        ValueError: Invalid ``name`` or non-integer ``estimated_size_kb``.
    """
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("software entry is missing a valid 'name' field")

    size = data.get("estimated_size_kb")
    estimated_size_kb: Optional[int]
    if size is None or size == "":
        estimated_size_kb = None
    else:
        try:
            estimated_size_kb = int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid estimated_size_kb: {size!r}") from exc

    system_component = data.get("system_component", False)
    if isinstance(system_component, str):
        system_component = system_component.strip().lower() in {"1", "true", "yes"}
    else:
        system_component = bool(system_component)

    return SoftwareEntry(
        name=name.strip(),
        version=_optional_str(data.get("version")),
        publisher=_optional_str(data.get("publisher")),
        install_date=_optional_str(data.get("install_date")),
        install_location=_optional_str(data.get("install_location")),
        estimated_size_kb=estimated_size_kb,
        scope=_optional_str(data.get("scope")) or "unknown",
        architecture=_optional_str(data.get("architecture")) or "unknown",
        uninstall_string=_optional_str(data.get("uninstall_string")),
        quiet_uninstall_string=_optional_str(data.get("quiet_uninstall_string")),
        registry_path=_optional_str(data.get("registry_path")) or "",
        release_type=_optional_str(data.get("release_type")),
        system_component=system_component,
        source=(_optional_str(data.get("source")) or SOURCE_REGISTRY).lower(),
    )


def _optional_str(value: Any) -> Optional[str]:
    """
    Coerce a JSON field to a stripped string or None.

    Args:
        value (Any): Raw JSON value.

    Returns:
        str | None: Non-empty stripped text, or None.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_entries_from_payload(payload: Any) -> list[SoftwareEntry]:
    """
    Accept either a legacy top-level array or a v1.1/v1.2 report envelope.

    Args:
        payload (Any): Parsed JSON value.

    Returns:
        list[SoftwareEntry]: Software rows. Non-dict elements inside arrays are
        skipped silently (malformed items do not abort the whole file).

    Raises:
        ValueError: Wrong top-level type, missing/unsupported ``schema_version``,
            or envelope without a ``software`` array.
    """
    if isinstance(payload, list):
        return [entry_from_dict(item) for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise ValueError(
            "JSON inventory must be a list of entries or a report object "
            "with 'schema_version' and 'software'"
        )

    schema = payload.get("schema_version")
    if schema is None:
        raise ValueError(
            "report object is missing 'schema_version'; "
            "use a legacy JSON array or a v1.1/v1.2 envelope"
        )
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"unsupported schema_version {schema!r}; "
            f"supported: {', '.join(sorted(SUPPORTED_SCHEMA_VERSIONS))}"
        )

    software = payload.get("software")
    if not isinstance(software, list):
        raise ValueError("report object must contain a 'software' array")

    return [entry_from_dict(item) for item in software if isinstance(item, dict)]
