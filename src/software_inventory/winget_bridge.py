"""
Winget reinstall bridge — map inventory rows to ``winget import`` packages.

P0 companion to the Uninstall-key audit: keep this tool as the safe census,
and emit a packages.schema.2.0 JSON file plus a checklist of apps winget
cannot reinstall.

    SoftwareEntry list + winget list (live or fixture)
        → match by normalized display name
        → importable PackageIdentifier rows → winget import JSON
        → unmatched inventory rows → Markdown checklist

Never installs, upgrades, or uninstalls software. Live mode only runs
``winget list`` (read-only inventory of what winget already knows).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from software_inventory.models import SoftwareEntry

logger = logging.getLogger(__name__)

WINGET_PACKAGES_SCHEMA = "https://aka.ms/winget-packages.schema.2.0.json"
DEFAULT_WINGET_SOURCE = {
    "Name": "winget",
    "Identifier": "Microsoft.Winget.Source_8wekyb3d8bbwe",
    "Argument": "https://cdn.winget.microsoft.com/cache",
    "Type": "Microsoft.PreIndexed.Package",
}
_IMPORTABLE_SOURCES = frozenset({"winget", "msstore"})
_NON_IMPORTABLE_ID_PREFIXES = ("ARP\\", "MSIX\\", "arp\\", "msix\\")
_TRAILING_VERSION = re.compile(
    r"[\s\-_]*(?:v(?:er(?:sion)?)?[\s\-_]*)?\d+(?:\.\d+){0,3}(?:\+\d+)?$",
    re.IGNORECASE,
)
WINGET_LIST_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class WingetPackage:
    """
    One row from ``winget list`` (or an equivalent fixture).

    Attributes:
        name: Display name as winget prints it.
        package_id: ``PackageIdentifier`` (catalog Id, or ``ARP\\`` /
            ``MSIX\\`` synthetic Id for locally discovered apps).
        version: Installed version, when winget reports one.
        source: ``winget`` / ``msstore`` for catalog rows, else empty.
    """

    name: str
    package_id: str
    version: Optional[str] = None
    source: str = ""

    def is_importable(self) -> bool:
        """
        Return True when this row can appear in a ``winget import`` document.

        Returns:
            bool: True for catalog-backed Ids (typically ``Publisher.Product``
            with Source ``winget`` / ``msstore``); False for empty Ids and
            ARP\\ / MSIX\\ synthetic Ids, which are discovery-only.
        """
        package_id = (self.package_id or "").strip()
        if not package_id:
            return False
        if package_id.startswith(_NON_IMPORTABLE_ID_PREFIXES):
            return False
        source = (self.source or "").strip().lower()
        if source in _IMPORTABLE_SOURCES:
            return True
        # Some hosts leave Source blank but still emit a real Id.
        return "." in package_id and "\\" not in package_id


@dataclass(frozen=True)
class MatchedPackage:
    """
    One inventory entry successfully mapped to an importable winget Id.

    Attributes:
        entry: Inventory row that was matched.
        package: Importable winget package chosen for it.
    """

    entry: SoftwareEntry
    package: WingetPackage


@dataclass(frozen=True)
class WingetBridgeResult:
    """
    Outcome of mapping an inventory against a winget package list.

    Attributes:
        matched: Inventory rows paired with an importable package.
        unmatched: Inventory rows winget cannot restore (checklist input).
        winget_packages: Every package winget reported, importable or not.
    """

    matched: tuple[MatchedPackage, ...]
    unmatched: tuple[SoftwareEntry, ...]
    winget_packages: tuple[WingetPackage, ...]

    @property
    def matched_count(self) -> int:
        """
        Number of inventory rows mapped to an importable package Id.

        Returns:
            int: ``len(matched)``.
        """
        return len(self.matched)

    @property
    def unmatched_count(self) -> int:
        """
        Number of inventory rows with no importable winget match.

        Returns:
            int: ``len(unmatched)``.
        """
        return len(self.unmatched)


def normalize_match_key(value: Optional[str]) -> str:
    """
    Normalize a display name for inventory ↔ winget matching.

    Args:
        value (str | None): Display name from either side of the match.

    Returns:
        str: Lowercased, whitespace-collapsed name with a trailing version
        token removed, so ``CapCut`` and ``CapCut 9.3.0`` meet. Empty for
        blank input; the unstripped text when stripping would leave nothing.
    """
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    stripped = _TRAILING_VERSION.sub("", text).strip(" -_")
    return stripped or text


def parse_winget_list_table(text: str) -> list[WingetPackage]:
    """
    Parse tabular ``winget list`` stdout into ``WingetPackage`` rows.

    Args:
        text (str): Raw console output from ``winget list``.

    Returns:
        list[WingetPackage]: Parsed rows (may be empty).

    Raises:
        ValueError: When the header/separator layout cannot be located.
    """
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    header_idx = None
    for index, line in enumerate(lines):
        if (
            "Name" in line
            and "Id" in line
            and "Version" in line
            and not line.lstrip().startswith("-")
        ):
            header_idx = index
            break
    if header_idx is None:
        raise ValueError("winget list output is missing a Name/Id/Version header")

    header = lines[header_idx]
    columns = _column_slices(header)
    start = header_idx + 1
    if start < len(lines) and set(lines[start].strip()) <= {"-", " "}:
        start += 1

    packages: list[WingetPackage] = []
    for line in lines[start:]:
        if line.lstrip().startswith("---"):
            continue
        # Progress / agreement noise occasionally appears mid-stream.
        if "\x08" in line or line.startswith("  "):
            continue
        name = _slice(line, columns["Name"]).strip()
        package_id = _slice(line, columns["Id"]).strip()
        if not name or not package_id:
            continue
        version = _slice(line, columns.get("Version")).strip() or None
        source = _extract_source(line, columns)
        packages.append(
            WingetPackage(
                name=name,
                package_id=package_id,
                version=version,
                source=source,
            )
        )
    return packages


def _extract_source(
    line: str,
    columns: dict[str, tuple[int, Optional[int]]],
) -> str:
    """
    Read the Source column, tolerating winget's occasional off-by-one alignment.

    Args:
        line (str): One data line of ``winget list`` output.
        columns (dict[str, tuple[int, int | None]]): Header slices from
            ``_column_slices``.

    Returns:
        str: Source name (``winget`` / ``msstore`` / other), or ``""``.

    Notes:
        Winget pads ``Available`` / ``Source`` inconsistently across hosts;
        taking tokens after the Version column is more reliable than a fixed
        slice alone.
    """
    known = _IMPORTABLE_SOURCES | {"winget", "msstore"}
    version_span = columns.get("Version")
    if version_span is not None and version_span[1] is not None:
        tail = line[version_span[1] :].strip()
        tokens = tail.split()
        if tokens and tokens[-1].lower() in known:
            return tokens[-1]
    source_span = columns.get("Source")
    if source_span is None:
        return ""
    direct = _slice(line, source_span).strip()
    if direct:
        return direct
    # Off-by-one: include one character before the header's Source start.
    start = source_span[0]
    if start > 0:
        widened = line[start - 1 :].strip()
        if widened:
            return widened.split()[0]
    return ""


def _column_slices(header: str) -> dict[str, tuple[int, Optional[int]]]:
    """
    Map winget list header labels to [start, end) character slices.

    Args:
        header (str): The ``Name  Id  Version ...`` header line.

    Returns:
        dict[str, tuple[int, int | None]]: Label to ``(start, end)``; the last
        column's end is None (runs to end of line).

    Raises:
        ValueError: When the header lacks a Name or Id column.
    """
    labels = ("Name", "Id", "Version", "Available", "Source")
    starts: dict[str, int] = {}
    for label in labels:
        pos = header.find(label)
        if pos >= 0:
            starts[label] = pos
    if "Name" not in starts or "Id" not in starts:
        raise ValueError("winget list header is missing Name or Id columns")

    ordered = sorted(starts.items(), key=lambda item: item[1])
    slices: dict[str, tuple[int, Optional[int]]] = {}
    for index, (label, start) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else None
        slices[label] = (start, end)
    return slices


def _slice(line: str, span: Optional[tuple[int, Optional[int]]]) -> str:
    """
    Extract one fixed-width column from a winget list data line.

    Args:
        line (str): Data line to slice.
        span (tuple[int, int | None] | None): Column slice, or None when the
            header did not have that column.

    Returns:
        str: Raw (unstripped) column text, or ``""`` when out of range.
    """
    if span is None:
        return ""
    start, end = span
    if start >= len(line):
        return ""
    return line[start:] if end is None else line[start:end]


def load_winget_packages_json(path: Path) -> list[WingetPackage]:
    """
    Load a winget package fixture JSON file for offline / CI matching.

    Accepted shapes:
        * Top-level array of objects with ``name`` / ``id`` (or
          ``package_id``) / optional ``version`` / ``source``.
        * Object with a ``packages`` array of the same objects.

    Args:
        path (Path): Fixture path.

    Returns:
        list[WingetPackage]: Parsed packages.

    Raises:
        ValueError: On missing file, invalid JSON, or unexpected shape.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read winget list fixture: {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid winget list JSON in {path}: {exc}") from exc

    if isinstance(payload, dict) and "packages" in payload:
        rows = payload["packages"]
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError(
            f"winget list fixture must be a JSON array or "
            f"{{'packages': [...]}} object: {path}"
        )

    packages: list[WingetPackage] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"winget list fixture row {index} is not an object")
        name = str(row.get("name") or "").strip()
        package_id = str(row.get("id") or row.get("package_id") or "").strip()
        if not name or not package_id:
            raise ValueError(
                f"winget list fixture row {index} requires name and id/package_id"
            )
        version = row.get("version")
        version_text = str(version).strip() if version is not None else None
        source = str(row.get("source") or "").strip()
        packages.append(
            WingetPackage(
                name=name,
                package_id=package_id,
                version=version_text or None,
                source=source,
            )
        )
    return packages


def resolve_winget_executable() -> Optional[str]:
    """
    Locate the ``winget`` executable on PATH (and the WindowsApps alias).

    Returns:
        str | None: Executable path, or None when winget is unavailable.
    """
    found = shutil.which("winget")
    if found:
        return found
    # App Execution Alias used when ``winget`` is not yet on PATH.
    alias = Path.home() / r"AppData\Local\Microsoft\WindowsApps\winget.exe"
    if alias.is_file():
        return str(alias)
    return None


def run_winget_list(
    *,
    winget_exe: Optional[str] = None,
    timeout: int = WINGET_LIST_TIMEOUT_SECONDS,
) -> list[WingetPackage]:
    """
    Run ``winget list`` and parse the tabular result.

    Args:
        winget_exe (str | None): Optional explicit winget path.
        timeout (int): Subprocess timeout in seconds.

    Returns:
        list[WingetPackage]: Parsed installed packages known to winget.

    Raises:
        FileNotFoundError: When winget is not installed / not on PATH.
        RuntimeError: When winget exits non-zero or output cannot be parsed.
        subprocess.TimeoutExpired: When the listing exceeds ``timeout``.
    """
    exe = winget_exe or resolve_winget_executable()
    if not exe:
        raise FileNotFoundError(
            "winget was not found on PATH. Install App Installer / Windows "
            "Package Manager, or pass --winget-list with a fixture JSON file."
        )
    argv = [
        exe,
        "list",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    logger.debug("Running %s", " ".join(argv))
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    # winget list often returns 0 even with unmatched ARP noise on stderr.
    if completed.returncode not in (0,):
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"winget list failed with exit code {completed.returncode}"
            + (f": {detail[:500]}" if detail else "")
        )
    try:
        return parse_winget_list_table(completed.stdout or "")
    except ValueError as exc:
        raise RuntimeError(f"failed to parse winget list output: {exc}") from exc


def match_inventory_to_winget(
    entries: Sequence[SoftwareEntry],
    packages: Sequence[WingetPackage],
) -> WingetBridgeResult:
    """
    Map inventory rows to importable winget packages by display name.

    Matching prefers exact normalized keys, then a conservative containment
    check (longer name contains the shorter, minimum length 4) so
    ``Visual Studio Code`` can meet ``Microsoft Visual Studio Code`` without
    matching every short token.

    Args:
        entries (Sequence[SoftwareEntry]): Prepared inventory rows.
        packages (Sequence[WingetPackage]): Parsed ``winget list`` rows.

    Returns:
        WingetBridgeResult: Matched pairs and unmatched inventory entries.
    """
    importable = [pkg for pkg in packages if pkg.is_importable()]
    by_key: dict[str, list[WingetPackage]] = {}
    for pkg in importable:
        key = normalize_match_key(pkg.name)
        if key:
            by_key.setdefault(key, []).append(pkg)

    matched: list[MatchedPackage] = []
    unmatched: list[SoftwareEntry] = []
    used_ids: set[str] = set()

    for entry in entries:
        package = _find_package(entry, by_key, used_ids)
        if package is None:
            unmatched.append(entry)
            continue
        used_ids.add(package.package_id)
        matched.append(MatchedPackage(entry=entry, package=package))

    return WingetBridgeResult(
        matched=tuple(matched),
        unmatched=tuple(unmatched),
        winget_packages=tuple(packages),
    )


def _find_package(
    entry: SoftwareEntry,
    by_key: dict[str, list[WingetPackage]],
    used_ids: set[str],
) -> Optional[WingetPackage]:
    """
    Resolve the best unused importable package for one inventory entry.

    Args:
        entry (SoftwareEntry): Inventory row to match.
        by_key (dict[str, list[WingetPackage]]): Importable packages grouped
            by ``normalize_match_key`` of their names.
        used_ids (set[str]): Package Ids already assigned to earlier entries.

    Returns:
        WingetPackage | None: Exact-key match first, else the longest
        containment overlap (keys of at least 4 characters), else None.
    """
    key = normalize_match_key(entry.name)
    if not key:
        return None

    candidates = by_key.get(key, [])
    for pkg in candidates:
        if pkg.package_id not in used_ids:
            return pkg

    # Containment fallback: prefer longest key overlap.
    best: Optional[WingetPackage] = None
    best_score = 0
    for other_key, pkgs in by_key.items():
        if len(key) < 4 or len(other_key) < 4:
            continue
        if key == other_key:
            continue
        if key in other_key or other_key in key:
            score = min(len(key), len(other_key))
            for pkg in pkgs:
                if pkg.package_id in used_ids:
                    continue
                if score > best_score:
                    best = pkg
                    best_score = score
    return best


def build_winget_import_document(
    result: WingetBridgeResult,
    *,
    include_versions: bool = False,
    winget_version: Optional[str] = None,
    creation_date: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Build a packages.schema.2.0 document for ``winget import``.

    Args:
        result (WingetBridgeResult): Match outcome.
        include_versions (bool): When True, pin ``Version`` on each package.
        winget_version (str | None): Optional WinGetVersion field.
        creation_date (datetime | None): Timestamp; defaults to UTC now.

    Returns:
        dict[str, Any]: JSON-serializable import document. When there are no
        matches, ``Sources`` is an empty list (import would be a no-op).
    """
    stamped = creation_date or datetime.now(timezone.utc)
    # winget examples use an offset style timestamp.
    creation = stamped.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "-00:00"

    packages: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in result.matched:
        package_id = item.package.package_id
        if package_id in seen:
            continue
        seen.add(package_id)
        row: dict[str, str] = {"PackageIdentifier": package_id}
        if include_versions:
            version = item.package.version or item.entry.version
            if version:
                row["Version"] = version
        packages.append(row)

    document: dict[str, Any] = {
        "$schema": WINGET_PACKAGES_SCHEMA,
        "CreationDate": creation,
        "Sources": [],
    }
    if winget_version:
        document["WinGetVersion"] = winget_version
    if packages:
        document["Sources"] = [
            {
                "SourceDetails": dict(DEFAULT_WINGET_SOURCE),
                "Packages": packages,
            }
        ]
    return document


def format_unmatched_markdown(
    unmatched: Sequence[SoftwareEntry],
    *,
    matched_count: int = 0,
) -> str:
    """
    Render a human reinstall checklist for apps winget cannot import.

    Args:
        unmatched (Sequence[SoftwareEntry]): Inventory rows without a package Id.
        matched_count (int): Matched count for the summary header.

    Returns:
        str: Markdown checklist text ending with a newline.
    """
    lines = [
        "# Unmatched software (manual reinstall checklist)",
        "",
        f"Matched to winget: **{matched_count}** - Unmatched: **{len(unmatched)}**",
        "",
        "These applications appeared in the Uninstall inventory but could not",
        "be mapped to a catalog `PackageIdentifier`. Install them manually",
        "(vendor installer, Store, portable copy, etc.).",
        "",
    ]
    if not unmatched:
        lines.append("_All inventory rows matched an importable winget package._")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "| Reinstalled? | Name | Version | Publisher | Install location |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for entry in sorted(unmatched, key=lambda row: row.name.lower()):
        name = _md_cell(entry.name)
        version = _md_cell(entry.version or "")
        publisher = _md_cell(entry.publisher or "")
        location = _md_cell(entry.install_location or "")
        lines.append(f"| [ ] | {name} | {version} | {publisher} | {location} |")
    lines.append("")
    return "\n".join(lines)


def _md_cell(value: str) -> str:
    """
    Escape pipe characters so Markdown tables stay intact.

    Args:
        value (str): Raw cell text.

    Returns:
        str: Text with ``|`` escaped and newlines flattened to spaces.
    """
    return value.replace("|", "\\|").replace("\n", " ")


def default_unmatched_path(winget_output: Path) -> Path:
    """
    Derive a sidecar checklist path next to a winget import JSON file.

    Args:
        winget_output (Path): Path passed to ``--output`` for ``--format winget``.

    Returns:
        Path: ``packages.unmatched.md`` beside ``packages.json``.
    """
    return winget_output.with_name(f"{winget_output.stem}.unmatched.md")
