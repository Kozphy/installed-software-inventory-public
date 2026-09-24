"""
Unit tests for table, JSON, and CSV exporters.

Covers the final pipeline stage: UTF-8 file writes, directory creation,
legacy JSON arrays, and human-readable table sizing — no live Registry.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from software_inventory.exporters import (
    ensure_parent_directory,
    export_csv,
    export_json,
    format_table,
)
from software_inventory.models import SoftwareEntry


def make_entry(**overrides) -> SoftwareEntry:
    """
    Build a ``SoftwareEntry`` fixture for exporter tests.

    Args:
        **overrides: Field overrides applied on top of the Sample App template.

    Returns:
        SoftwareEntry: Immutable test row.
    """
    data = {
        "name": "Sample App",
        "version": "3.2.1",
        "publisher": "Sample Publisher",
        "install_date": "2026-07-17",
        "install_location": r"C:\Program Files\Sample",
        "estimated_size_kb": 870400,
        "scope": "machine",
        "architecture": "64-bit",
        "uninstall_string": r"C:\Program Files\Sample\uninstall.exe",
        "quiet_uninstall_string": r"C:\Program Files\Sample\uninstall.exe /S",
        "registry_path": r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Sample",
        "release_type": None,
        "system_component": False,
    }
    data.update(overrides)
    return SoftwareEntry(**data)


class ExporterTests(unittest.TestCase):
    """JSON/CSV/table export shapes and UTF-8 file I/O."""

    def setUp(self) -> None:
        """Build a small two-entry inventory fixture for exporter assertions."""
        self.entries = [
            make_entry(),
            make_entry(
                name="Other App",
                version="1.0",
                publisher="Other Co",
                estimated_size_kb=512,
                install_date=None,
                architecture="32-bit",
                scope="current_user",
                registry_path=r"HKEY_CURRENT_USER\SOFTWARE\...\Other",
            ),
        ]

    def test_json_export_roundtrip(self) -> None:
        """JSON export writes a file that parses back to the same rows."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out" / "software.json"
            export_json(self.entries, output=path, pretty=True)
            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 2)
            self.assertEqual(payload[0]["name"], "Sample App")
            self.assertEqual(payload[0]["estimated_size_kb"], 870400)
            self.assertIn("registry_path", payload[0])

    def test_json_compact_when_not_pretty(self) -> None:
        """Without ``pretty`` the JSON has no indentation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "software.json"
            export_json(self.entries, output=path, pretty=False)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\n  ", text)

    def test_csv_export_utf8_and_headers(self) -> None:
        """CSV export is UTF-8 with the public header contract."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports" / "software.csv"
            export_csv(self.entries, output=path)
            self.assertTrue(path.is_file())
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(reader.fieldnames[0], "name")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["name"], "Sample App")
            self.assertEqual(rows[0]["estimated_size_kb"], "870400")
            self.assertEqual(rows[1]["install_date"], "")

    def test_output_directory_creation(self) -> None:
        """Missing parent directories are created before writing."""
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b" / "c" / "inventory.json"
            self.assertFalse(nested.parent.exists())
            ensure_parent_directory(nested)
            self.assertTrue(nested.parent.is_dir())
            export_json(self.entries, output=nested)
            self.assertTrue(nested.is_file())

    def test_table_contains_columns_and_human_size(self) -> None:
        """The table shows column headers and human-readable sizes."""
        text = format_table(self.entries)
        self.assertIn("Name", text)
        self.assertIn("Version", text)
        self.assertIn("Publisher", text)
        self.assertIn("Sample App", text)
        self.assertIn("850 MB", text)
        self.assertIn("512 KB", text)

    def test_table_empty_state(self) -> None:
        """An empty inventory renders a friendly empty-state message."""
        text = format_table([])
        self.assertIn("no matching software entries", text)

    def test_long_names_are_truncated(self) -> None:
        """Overlong names are truncated with an ellipsis."""
        long_name = "A" * 80
        text = format_table([make_entry(name=long_name)])
        # Truncation marker should appear; full 80-char name should not as one cell.
        self.assertIn("…", text)
        first_data_line = text.splitlines()[2]
        self.assertLess(len(first_data_line), 200)


if __name__ == "__main__":
    unittest.main()
