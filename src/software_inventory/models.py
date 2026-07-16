"""Data models for installed software inventory records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Optional


@dataclass(frozen=True)
class SoftwareEntry:
    """A single installed application discovered from the Windows Registry."""

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary representation."""
        return asdict(self)

    def completeness_score(self) -> int:
        """Count non-empty optional metadata fields for deduplication ranking."""
        score = 0
        for field in fields(self):
            if field.name in {"name", "registry_path", "scope", "architecture", "system_component"}:
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
)
