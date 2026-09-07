"""
Prepare stage: normalize helpers, filter policy, and deduplication.

Sits between collectors and exporters:

    raw SoftwareEntry list (already field-normalized by the collector)
        → deduplicate overlapping hive views (version *included* in key)
        → drop blank names / system components / updates (unless opted in)
        → optional ``--search`` substring filter
        → alphabetical sort
        → prepared list + PrepareStats for the v1.1 report envelope

Low-level ``normalize_*`` helpers are also called while reading Registry
values; this module stays I/O-free so tests can drive it with fixtures only.

Important design contrast with ``diff.identity_key``:
    prepare dedupe keys on (name, version, publisher, location) so two
    versions of the same product stay distinct rows in a single scan;
    snapshot diff keys on (name, publisher, location) so a version bump
    between scans is a *change*, not remove+add.
"""


from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable, Optional, Sequence

from software_inventory.models import SoftwareEntry
from software_inventory.report import PrepareStats

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
    """
    Coerce a Registry value to stripped text, treating blanks as missing.

    Keeps exporters from treating whitespace-only Uninstall fields as real
    publisher/version metadata.

    Args:
        value (object | None): Raw Registry value (str, bytes, int, etc.).

    Returns:
        str | None: Stripped text, or None when missing/blank. Bytes are decoded
        with ``errors='replace'``; only unexpected decode failures yield None.
    """
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
    """
    Return ``YYYY-MM-DD`` when the calendar date is valid; otherwise None.

    Years before 1980 are rejected as implausible install dates for modern
    Windows Uninstall metadata.

    Args:
        year (int): Four-digit year.
        month (int): Month 1–12.
        day (int): Day of month.

    Returns:
        str | None: ISO date string, or None if invalid/out of range.
    """
    if year < 1980:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_install_date(value: object | None) -> Optional[str]:
    """
    Normalize Uninstall ``InstallDate`` into ``YYYY-MM-DD`` for audits/diffs.

    Windows commonly stores compact ``YYYYMMDD``. Invalid values become None
    (never raise) so one corrupt key cannot abort a full machine scan.

    Args:
        value (object | None): Raw ``InstallDate`` Registry value.

    Returns:
        str | None: Calendar date, or None when unparseable / pre-1980 / invalid
        day-of-month (e.g. ``20260230``).

    Notes:
        Parsing order: digit-stripped 8-char compact form, then ISO
        ``YYYY-MM-DD``, then a few locale formats (``%m/%d/%Y``, etc.).
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
    """
    Parse Registry ``EstimatedSize`` (stored in KB) as a non-negative integer.

    Args:
        value (object | None): Raw EstimatedSize DWORD or string.

    Returns:
        int | None: Size in KB, or None when missing/invalid/negative.
    """
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
    """
    Format a size in KB as a human-readable string for table output.

    Args:
        size_kb (int | None): Size from ``estimated_size_kb``.

    Returns:
        str: Empty string when unknown; otherwise KB/MB/GB display text.
    """
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
    """
    Interpret the Registry ``SystemComponent`` DWORD as a boolean.

    System components are hidden by default so personal audits focus on
    user-facing applications rather than OS plumbing.

    Args:
        value (object | None): Raw SystemComponent value.

    Returns:
        bool: True when the value indicates a system component.
    """
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
    """
    Heuristic: treat KB/security-update style rows as Windows update noise.

    Default exports hide these so a personal inventory is not dominated by
    patch entries. Pass ``--include-updates`` when auditing patch coverage.

    Args:
        entry (SoftwareEntry): Candidate inventory record.

    Returns:
        bool: True when ``release_type`` is an update-like token, or the display
        name matches patterns such as ``KBnnnnnnn``, ``Security Update…``,
        ``Update for…``, or ``Hotfix…``.

    Notes:
        Name patterns can false-positive on non-Microsoft products titled
        "Update for …". Prefer ``release_type`` when both are present.
    """
    release = (entry.release_type or "").strip().lower()
    if release in _RELEASE_UPDATE_TYPES:
        return True

    name = entry.name or ""
    for pattern in _UPDATE_PATTERNS:
        if pattern.search(name):
            return True
    return False


def has_valid_display_name(entry: SoftwareEntry) -> bool:
    """
    Check that the entry has a non-empty display name.

    Nameless Uninstall subkeys are dropped by design; they are not useful in
    audits and often represent incomplete installer residue.

    Args:
        entry (SoftwareEntry): Candidate inventory record.

    Returns:
        bool: True when ``name`` is non-blank after strip.
    """
    return bool(entry.name and entry.name.strip())


def filter_entries(
    entries: Iterable[SoftwareEntry],
    *,
    include_system_components: bool = False,
    include_updates: bool = False,
    search: Optional[str] = None,
) -> list[SoftwareEntry]:
    """
    Apply display-name, system-component, update, and search filters.

    Args:
        entries (Iterable[SoftwareEntry]): Entries to filter.
        include_system_components (bool): Keep SystemComponent rows when True.
        include_updates (bool): Keep Windows updates/hotfixes when True.
        search (str | None): Case-insensitive needle for name/publisher/version.

    Returns:
        list[SoftwareEntry]: Entries that pass all active filters.
    """
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
    """
    Case-insensitive substring match across name, publisher, and version.

    Powers the CLI ``--search`` flag for quick audits (e.g. ``microsoft``).

    Args:
        entry (SoftwareEntry): Entry to test.
        needle (str): Search text (already expected lowercased by callers, but
            lowered again defensively).

    Returns:
        bool: True when any of name/publisher/version contains the needle.
    """
    lowered = needle.lower()
    haystacks = (
        entry.name or "",
        entry.publisher or "",
        entry.version or "",
    )
    return any(lowered in value.lower() for value in haystacks)


def deduplication_key(entry: SoftwareEntry) -> tuple[str, str, str, str]:
    """
    Key used to collapse duplicate Uninstall views *within one scan*.

    32-bit/64-bit hive views and ``WOW6432Node`` often list the same product
    twice. Including **version** keeps simultaneous side-by-side installs
    (e.g. 1.0 and 2.0) as separate rows — unlike ``diff.identity_key``, which
    omits version so upgrades between snapshots show as changes.

    Args:
        entry (SoftwareEntry): Entry to key.

    Returns:
        tuple[str, str, str, str]: Lowercased
        ``(name, version, publisher, install_location)`` with trailing
        ``\\`` / ``/`` stripped from the location.
    """
    return (
        (entry.name or "").strip().lower(),
        (entry.version or "").strip().lower(),
        (entry.publisher or "").strip().lower(),
        (entry.install_location or "").strip().lower().rstrip("\\/"),
    )


def _prefer_entry(current: SoftwareEntry, candidate: SoftwareEntry) -> SoftwareEntry:
    """
    Prefer the record with richer metadata; break ties on registry path.

    Args:
        current (SoftwareEntry): Entry already chosen for a dedupe key.
        candidate (SoftwareEntry): Competing entry with the same key.

    Returns:
        SoftwareEntry: The richer (or lexicographically earlier-path) entry.
    """
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
    """
    Remove duplicate entries, keeping the most complete record per key.

    Args:
        entries (Sequence[SoftwareEntry]): Possibly overlapping collector output.

    Returns:
        list[SoftwareEntry]: Deduplicated entries (order is insertion order of
        first-seen keys before later sort stages).
    """
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
    """
    Sort entries alphabetically by application name (case-insensitive).

    Args:
        entries (Iterable[SoftwareEntry]): Entries to sort.

    Returns:
        list[SoftwareEntry]: Stable secondary sort by ``registry_path``.
    """
    return sorted(entries, key=lambda e: ((e.name or "").lower(), e.registry_path))


def prepare_inventory(
    entries: Sequence[SoftwareEntry],
    *,
    include_system_components: bool = False,
    include_updates: bool = False,
    search: Optional[str] = None,
) -> list[SoftwareEntry]:
    """
    Deduplicate, filter, and sort inventory entries for export.

    Convenience wrapper when callers do not need audit counters.

    Args:
        entries (Sequence[SoftwareEntry]): Raw collector output.
        include_system_components (bool): Keep SystemComponent rows when True.
        include_updates (bool): Keep Windows updates/hotfixes when True.
        search (str | None): Optional case-insensitive search needle.

    Returns:
        list[SoftwareEntry]: Ready-to-export inventory rows.
    """
    prepared, _stats = prepare_inventory_with_stats(
        entries,
        include_system_components=include_system_components,
        include_updates=include_updates,
        search=search,
    )
    return prepared


def prepare_inventory_with_stats(
    entries: Sequence[SoftwareEntry],
    *,
    include_system_components: bool = False,
    include_updates: bool = False,
    search: Optional[str] = None,
) -> tuple[list[SoftwareEntry], PrepareStats]:
    """
    Deduplicate, filter, and sort while recording counts for scan metadata.

    ``PrepareStats`` powers v1.1 ``scan.*`` fields so a JSON snapshot explains
    how aggressive the default filters were (useful for fleet trust).

    Args:
        entries (Sequence[SoftwareEntry]): Raw collector output.
        include_system_components (bool): Keep SystemComponent rows when True.
        include_updates (bool): Keep update/hotfix rows when True.
        search (str | None): Optional case-insensitive name/publisher/version
            substring; applied after system/update filters.

    Returns:
        tuple[list[SoftwareEntry], PrepareStats]: Sorted rows plus counters.

    Notes:
        ``filtered_system_component_count`` / ``filtered_update_count`` only
        count those two filter reasons. Blank-name drops and ``--search`` misses
        are *not* reflected in those counters (they still reduce ``result_count``).
    """
    raw_list = list(entries)
    unique = deduplicate_entries(raw_list)

    filtered_system = 0
    filtered_updates = 0
    retained: list[SoftwareEntry] = []
    needle = search.strip().lower() if search else None

    for entry in unique:
        if not has_valid_display_name(entry):
            continue
        if not include_system_components and entry.system_component:
            filtered_system += 1
            continue
        if not include_updates and is_windows_update(entry):
            filtered_updates += 1
            continue
        if needle is not None and not matches_search(entry, needle):
            continue
        retained.append(entry)

    prepared = sort_entries(retained)
    stats = PrepareStats(
        raw_entry_count=len(raw_list),
        after_dedup_count=len(unique),
        filtered_system_component_count=filtered_system,
        filtered_update_count=filtered_updates,
        result_count=len(prepared),
    )
    return prepared, stats
