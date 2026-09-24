"""
Export prepared inventory (and diffs) as table, JSON, CSV, or winget import.

Last pipeline stage — no Registry access:

    prepared SoftwareEntry / DiffResult / WingetBridgeResult
        → UTF-8 file (parents created) or console
        → table | JSON envelope/array | CSV | diff | winget import JSON

File outputs are always UTF-8. Console writes go through ``_safe_write`` so
non-ASCII display names degrade safely on legacy Windows code pages.
"""


from __future__ import annotations

import csv
import json
import shutil
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Optional, TextIO, Union

from software_inventory.diff import DiffResult, format_diff_table
from software_inventory.models import CSV_FIELDNAMES, SoftwareEntry
from software_inventory.normalize import format_size_human
from software_inventory.report import InventoryReport
from software_inventory.winget_bridge import (
    WingetBridgeResult,
    build_winget_import_document,
    format_unmatched_markdown,
)
TABLE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("Name", "name", 36),
    ("Version", "version", 14),
    ("Publisher", "publisher", 24),
    ("Scope", "scope", 12),
    ("Architecture", "architecture", 12),
    ("Install Date", "install_date", 12),
    ("Size", "size", 10),
)


def ensure_parent_directory(path: Path) -> None:
    """
    Create the parent directory of ``path`` when it does not already exist.

    Lets ``--output reports/foo.json`` succeed without a pre-created folder.

    Args:
        path (Path): Target file path whose parents should exist.
    """
    parent = path.parent
    if parent and str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)


def _cell(value: Optional[str], width: int) -> str:
    """
    Truncate and pad a cell so the console table layout stays stable.

    Args:
        value (str | None): Cell text.
        width (int): Fixed column width.

    Returns:
        str: Left-padded/truncated cell including an ellipsis when truncated.
    """
    text = value or ""
    if len(text) > width:
        if width <= 1:
            return text[:width]
        return text[: width - 1] + "…"
    return text.ljust(width)


def _row_values(entry: SoftwareEntry) -> dict[str, str]:
    """
    Map an entry to display strings for the console table.

    Args:
        entry (SoftwareEntry): Inventory row.

    Returns:
        dict[str, str]: Column key → display value (human size for Size).
    """
    return {
        "name": entry.name,
        "version": entry.version or "",
        "publisher": entry.publisher or "",
        "scope": entry.scope,
        "architecture": entry.architecture,
        "install_date": entry.install_date or "",
        "size": format_size_human(entry.estimated_size_kb),
    }


def format_table(entries: Iterable[SoftwareEntry]) -> str:
    """
    Render a readable fixed-width table for console output.

    Args:
        entries (Iterable[SoftwareEntry]): Prepared inventory rows.

    Returns:
        str: Multi-line table text (empty-state message when no rows).

    Notes:
        Shrinks the Name column when the terminal is narrower than the default
        layout so columns do not wrap poorly on small consoles.
    """
    rows = [_row_values(entry) for entry in entries]
    term_width = shutil.get_terminal_size((120, 24)).columns
    # Scale name column if the terminal is narrower than the default layout.
    default_total = sum(width for _, _, width in TABLE_COLUMNS) + (len(TABLE_COLUMNS) - 1) * 2
    name_width = TABLE_COLUMNS[0][2]
    if term_width < default_total:
        shrink = default_total - term_width
        name_width = max(16, name_width - shrink)

    widths = [name_width if key == "name" else width for _, key, width in TABLE_COLUMNS]
    headers = [label for label, _, _ in TABLE_COLUMNS]
    keys = [key for _, key, _ in TABLE_COLUMNS]

    lines: list[str] = []
    header_line = "  ".join(_cell(h, w) for h, w in zip(headers, widths))
    separator = "  ".join("-" * w for w in widths)
    lines.append(header_line.rstrip())
    lines.append(separator)

    if not rows:
        lines.append("(no matching software entries)")
        return "\n".join(lines)

    for row in rows:
        line = "  ".join(_cell(row[key], w) for key, w in zip(keys, widths))
        lines.append(line.rstrip())
    return "\n".join(lines)


def _safe_write(stream: TextIO, text: str) -> None:
    """
    Write text to a stream, replacing characters the console cannot encode.

    Protects non-ASCII display names (e.g. Japanese app names) on legacy
    Windows code pages when writing to stdout.

    Args:
        stream (TextIO): Destination stream.
        text (str): UTF-8 text to write.
    """
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        raw = text.encode(encoding, errors="replace")
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(raw)
            if hasattr(stream, "flush"):
                stream.flush()
        else:
            stream.write(raw.decode(encoding, errors="replace"))


def _write_text(text: str, output: Optional[Path], stream: Optional[TextIO]) -> None:
    """
    Write UTF-8 text to a file or stream.

    Args:
        text (str): Content to write.
        output (Path | None): File path when writing to disk.
        stream (TextIO | None): Stream override; defaults to stdout.
    """
    if output is not None:
        ensure_parent_directory(output)
        output.write_text(text, encoding="utf-8")
    else:
        target = stream or sys.stdout
        _safe_write(target, text)


def export_table(
    entries: Iterable[SoftwareEntry],
    output: Optional[Path] = None,
    stream: Optional[TextIO] = None,
) -> None:
    """
    Write table-formatted inventory to a file or stdout.

    Args:
        entries (Iterable[SoftwareEntry]): Prepared inventory rows.
        output (Path | None): Optional destination file.
        stream (TextIO | None): Optional stream (stdout when omitted).
    """
    _write_text(format_table(entries) + "\n", output, stream)


def entries_to_jsonable(entries: Iterable[SoftwareEntry]) -> list[dict]:
    """
    Convert entries to dictionaries suitable for JSON serialization.

    Args:
        entries (Iterable[SoftwareEntry]): Inventory rows.

    Returns:
        list[dict]: Flat dicts matching ``SoftwareEntry`` field names.
    """
    return [entry.to_dict() for entry in entries]


def dumps_json(payload: Any, *, pretty: bool = False) -> str:
    """
    Serialize ``payload`` to a UTF-8 JSON string.

    Args:
        payload (Any): JSON-serializable object.
        pretty (bool): Indent with 2 spaces when True.

    Returns:
        str: JSON text ending with a newline.
    """
    if pretty:
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"


def export_json(
    entries: Iterable[SoftwareEntry],
    output: Optional[Path] = None,
    *,
    pretty: bool = False,
    stream: Optional[TextIO] = None,
    report: Optional[InventoryReport] = None,
    legacy_json: bool = False,
) -> None:
    """
    Write inventory JSON for humans, scripts, and snapshot archives.

    Prefer the v1.1 envelope when ``report`` is provided (default CLI path).
    Emit a bare array when ``legacy_json=True`` **or** ``report is None``
    (unit tests / v1.0 consumers).

    Args:
        entries (Iterable[SoftwareEntry]): Prepared rows. Used for array mode;
            in envelope mode the software list comes from ``report.software``.
        output (Path | None): File path; parent dirs are created as needed.
            Files are always UTF-8. Console writes may replace unencodable
            glyphs on legacy code pages.
        pretty (bool): Indent with 2 spaces when True.
        stream (TextIO | None): Stream override; defaults to stdout.
        report (InventoryReport | None): Envelope to emit when not legacy.
        legacy_json (bool): Force top-level array output when True.
    """
    if legacy_json or report is None:
        payload: Union[list[dict], dict[str, Any]] = entries_to_jsonable(entries)
    else:
        payload = report.to_dict()
    _write_text(dumps_json(payload, pretty=pretty), output, stream)


def export_csv(
    entries: Iterable[SoftwareEntry],
    output: Optional[Path] = None,
    stream: Optional[TextIO] = None,
) -> None:
    """
    Write inventory as UTF-8 CSV to a file or stdout.

    Column order follows ``CSV_FIELDNAMES`` so spreadsheets stay stable across
    releases.

    Args:
        entries (Iterable[SoftwareEntry]): Prepared inventory rows.
        output (Path | None): Optional destination file.
        stream (TextIO | None): Optional stream (stdout when omitted).
    """
    rows = [entry.to_dict() for entry in entries]

    if output is not None:
        ensure_parent_directory(output)
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDNAMES))
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key) for key in CSV_FIELDNAMES})
        return

    target = stream or sys.stdout
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_FIELDNAMES), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in CSV_FIELDNAMES})
    _safe_write(target, buffer.getvalue())


def export_diff(
    result: DiffResult,
    *,
    format_name: str = "table",
    output: Optional[Path] = None,
    pretty: bool = False,
    stream: Optional[TextIO] = None,
) -> None:
    """
    Write a snapshot diff as table or JSON.

    Args:
        result (DiffResult): Comparison output from ``compare_inventories``.
        format_name (str): ``table`` or ``json``.
        output (Path | None): Optional destination file.
        pretty (bool): Pretty-print JSON when True.
        stream (TextIO | None): Optional stream (stdout when omitted).

    Raises:
        ValueError: When ``format_name`` is not supported.
    """
    if format_name == "json":
        _write_text(dumps_json(result.to_dict(), pretty=pretty), output, stream)
        return
    if format_name == "table":
        _write_text(format_diff_table(result), output, stream)
        return
    raise ValueError(f"unsupported diff format: {format_name!r}")


def export_winget(
    result: WingetBridgeResult,
    output: Optional[Path] = None,
    *,
    pretty: bool = True,
    include_versions: bool = False,
    unmatched_output: Optional[Path] = None,
    stream: Optional[TextIO] = None,
) -> None:
    """
    Write a ``winget import`` JSON document and optional unmatched checklist.

    Args:
        result (WingetBridgeResult): Match outcome from ``winget_bridge``.
        output (Path | None): Import JSON path; stdout when omitted.
        pretty (bool): Indent JSON (default True — import files are edited).
        include_versions (bool): Pin package versions in the import document.
        unmatched_output (Path | None): Markdown checklist path. When None,
            no checklist file is written (callers may still print a summary).
        stream (TextIO | None): Stream override for the JSON document.
    """
    document = build_winget_import_document(
        result,
        include_versions=include_versions,
    )
    _write_text(dumps_json(document, pretty=pretty), output, stream)
    if unmatched_output is not None:
        checklist = format_unmatched_markdown(
            result.unmatched,
            matched_count=result.matched_count,
        )
        ensure_parent_directory(unmatched_output)
        unmatched_output.write_text(checklist, encoding="utf-8")
