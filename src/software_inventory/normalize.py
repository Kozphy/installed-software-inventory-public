"""Normalization, filtering, search, and deduplication helpers."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable, Optional, Sequence

from software_inventory.models import SoftwareEntry

_UPDATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^KB\d+", re.IGNORECASE),
    re.compile(r"^Security Update\b", re.IGNORECASE),
    re.compile(r"^Update for\b", re.IGNORECASE),
    re.compile(r"^Hotfix for\b", re.IGNORECASE),
    re.compile(r"\bHotfix\b", re.IGNORECASE),
)

_RELEASE_UPDATE_TYPES = frozenset(
    {
        "security update",
        "update",
        "hotfix",
        "service pack",
    }
)


def normalize_string(value: object | None) -> Optional[str]:
    """Coerce a Registry value to a stripped string, or ``None`` if empty."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            return None
    else:
        text = str(value)
    text = text.strip()
    return text or None


def _validated_iso_date(year: int, month: int, day: int) -> Optional[str]:
    """Return ``YYYY-MM-DD`` when the calendar date is valid; otherwise ``None``."""
    if year < 1980:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_install_date(value: object | None) -> Optional[str]:
    """Convert Registry install dates such as ``20260717`` into ``YYYY-MM-DD``.

    Invalid or unknown values return ``None`` instead of raising.
    """
    text = normalize_string(value)
    if text is None:
        return None

    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        return _validated_iso_date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))

    # Already ISO-like: YYYY-MM-DD
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return _validated_iso_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    # Last resort: common locale date strings
    for fmt in ("%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return _validated_iso_date(parsed.year, parsed.month, parsed.day)
        except ValueError:
            continue
    return None


def normalize_estimated_size_kb(value: object | None) -> Optional[int]:
    """Parse Registry ``EstimatedSize`` (stored in KB) as a non-negative integer."""
    if value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        text = normalize_string(value)
        if text is None:
            return None
        try:
            size = int(text)
        except ValueError:
            return None
    if size < 0:
        return None
    return size


def format_size_human(size_kb: Optional[int]) -> str:
    """Format a size in KB as a human-readable string for table output."""
    if size_kb is None:
        return ""
    if size_kb < 1024:
        return f"{size_kb} KB"
    size_mb = size_kb / 1024
    if size_mb < 1024:
        if size_mb >= 100 or size_mb == int(size_mb):
            return f"{int(round(size_mb))} MB"
        return f"{size_mb:.1f} MB"
    size_gb = size_mb / 1024
    if size_gb >= 100 or size_gb == int(size_gb):
        return f"{int(round(size_gb))} GB"
    return f"{size_gb:.1f} GB"


def normalize_system_component(value: object | None) -> bool:
    """Interpret the Registry ``SystemComponent`` DWORD as a boolean."""
    if value is None:
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        text = normalize_string(value)
        if text is None:
            return False
        return text.lower() in {"1", "true", "yes"}


def is_windows_update(entry: SoftwareEntry) -> bool:
    """Return ``True`` when the entry looks like a Windows update or hotfix."""
    release = (entry.release_type or "").strip().lower()
    if release in _RELEASE_UPDATE_TYPES:
        return True

    name = entry.name or ""
    for pattern in _UPDATE_PATTERNS:
        if pattern.search(name):
            return True
    return False


def has_valid_display_name(entry: SoftwareEntry) -> bool:
    """Return ``True`` when the entry has a non-empty display name."""
    return bool(entry.name and entry.name.strip())


def filter_entries(
    entries: Iterable[SoftwareEntry],
    *,
    include_system_components: bool = False,
    include_updates: bool = False,
    search: Optional[str] = None,
) -> list[SoftwareEntry]:
    """Apply display-name, system-component, update, and search filters."""
    results: list[SoftwareEntry] = []
    needle = search.strip().lower() if search else None

    for entry in entries:
        if not has_valid_display_name(entry):
            continue
        if not include_system_components and entry.system_component:
            continue
        if not include_updates and is_windows_update(entry):
            continue
        if needle is not None and not matches_search(entry, needle):
            continue
        results.append(entry)
    return results


def matches_search(entry: SoftwareEntry, needle: str) -> bool:
    """Case-insensitive search across name, publisher, and version."""
    lowered = needle.lower()
    haystacks = (
        entry.name or "",
        entry.publisher or "",
        entry.version or "",
    )
    return any(lowered in value.lower() for value in haystacks)


def deduplication_key(entry: SoftwareEntry) -> tuple[str, str, str, str]:
    """Build a deterministic key from normalized identifying fields."""
    return (
        (entry.name or "").strip().lower(),
        (entry.version or "").strip().lower(),
        (entry.publisher or "").strip().lower(),
        (entry.install_location or "").strip().lower().rstrip("\\/"),
    )


def _prefer_entry(current: SoftwareEntry, candidate: SoftwareEntry) -> SoftwareEntry:
    """Prefer the record with richer metadata; break ties on registry path."""
    current_score = current.completeness_score()
    candidate_score = candidate.completeness_score()
    if candidate_score > current_score:
        return candidate
    if candidate_score < current_score:
        return current
    # Stable tie-breaker for deterministic output.
    if candidate.registry_path < current.registry_path:
        return candidate
    return current


def deduplicate_entries(entries: Sequence[SoftwareEntry]) -> list[SoftwareEntry]:
    """Remove duplicate entries, keeping the most complete record per key."""
    chosen: dict[tuple[str, str, str, str], SoftwareEntry] = {}
    for entry in entries:
        key = deduplication_key(entry)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = entry
        else:
            chosen[key] = _prefer_entry(existing, entry)
    return list(chosen.values())


def sort_entries(entries: Iterable[SoftwareEntry]) -> list[SoftwareEntry]:
    """Sort entries alphabetically by application name (case-insensitive)."""
    return sorted(entries, key=lambda e: ((e.name or "").lower(), e.registry_path))


def prepare_inventory(
    entries: Sequence[SoftwareEntry],
    *,
    include_system_components: bool = False,
    include_updates: bool = False,
    search: Optional[str] = None,
) -> list[SoftwareEntry]:
    """Deduplicate, filter, and sort inventory entries for export."""
    unique = deduplicate_entries(entries)
    filtered = filter_entries(
        unique,
        include_system_components=include_system_components,
        include_updates=include_updates,
        search=search,
    )
    return sort_entries(filtered)
