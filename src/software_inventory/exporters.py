"""Export inventory results as table, JSON, or CSV."""

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
    """Create the parent directory of ``path`` when it does not already exist."""
    parent = path.parent
    if parent and str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)


def _cell(value: Optional[str], width: int) -> str:
    """Truncate and pad a cell so the table layout stays stable."""
    text = value or ""
    if len(text) > width:
        if width <= 1:
            return text[:width]
        return text[: width - 1] + "…"
    return text.ljust(width)


def _row_values(entry: SoftwareEntry) -> dict[str, str]:
    """Map an entry to display strings for the console table."""
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
    """Render a readable fixed-width table for console output."""
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
    """Write text to a stream, replacing characters the console cannot encode."""
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
    """Write UTF-8 text to a file or stream."""
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
    """Write table-formatted inventory to a file or stdout."""
    _write_text(format_table(entries) + "\n", output, stream)


def entries_to_jsonable(entries: Iterable[SoftwareEntry]) -> list[dict]:
    """Convert entries to a list of dictionaries suitable for JSON serialization."""
    return [entry.to_dict() for entry in entries]


def dumps_json(payload: Any, *, pretty: bool = False) -> str:
    """Serialize ``payload`` to a UTF-8 JSON string."""
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
    """Write inventory as UTF-8 JSON to a file or stdout.

    By default a v1.1 report envelope is written when ``report`` is provided.
    Pass ``legacy_json=True`` (or omit ``report``) to emit a top-level array.
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
    """Write inventory as UTF-8 CSV to a file or stdout."""
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
    """Write a snapshot diff as table or JSON."""
    if format_name == "json":
        _write_text(dumps_json(result.to_dict(), pretty=pretty), output, stream)
        return
    if format_name == "table":
        _write_text(format_diff_table(result), output, stream)
        return
    raise ValueError(f"unsupported diff format: {format_name!r}")
