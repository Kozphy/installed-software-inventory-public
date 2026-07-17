"""Versioned inventory report envelope and scan metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from software_inventory.models import SoftwareEntry

SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.1"})


@dataclass(frozen=True)
class ScanMetadata:
    """Audit metadata describing a single inventory scan."""

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
        """Return a JSON-serializable dictionary."""
        data = asdict(self)
        data["collector_sources"] = list(self.collector_sources)
        return data


@dataclass(frozen=True)
class InventoryReport:
    """Versioned JSON report containing scan metadata and software entries."""

    schema_version: str
    scan: ScanMetadata
    software: tuple[SoftwareEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report envelope."""
        return {
            "schema_version": self.schema_version,
            "scan": self.scan.to_dict(),
            "software": [entry.to_dict() for entry in self.software],
        }


@dataclass
class PrepareStats:
    """Counts collected while preparing inventory results."""

    raw_entry_count: int = 0
    after_dedup_count: int = 0
    filtered_system_component_count: int = 0
    filtered_update_count: int = 0
    result_count: int = 0

    @property
    def deduplicated_count(self) -> int:
        """Number of entries removed by deduplication."""
        return max(0, self.raw_entry_count - self.after_dedup_count)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def duration_ms(started_at: datetime, completed_at: datetime) -> int:
    """Compute elapsed milliseconds between two aware datetimes."""
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
    """Build scan metadata from timing and prepare statistics."""
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
    """Assemble a versioned inventory report."""
    return InventoryReport(
        schema_version=schema_version,
        scan=scan,
        software=tuple(entries),
    )


def entry_from_dict(data: dict[str, Any]) -> SoftwareEntry:
    """Construct a ``SoftwareEntry`` from a JSON object.

    Missing optional fields become ``None`` / ``False``. A missing ``name``
    raises ``ValueError``.
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
    )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_entries_from_payload(payload: Any) -> list[SoftwareEntry]:
    """Load software entries from a legacy array or v1.1 report envelope.

    Raises
    ------
    ValueError
        When the payload shape or schema version is unsupported.
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
            "use a legacy JSON array or a v1.1 envelope"
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
