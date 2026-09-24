"""
Unit tests for versioned report envelopes and payload loading.

Covers v1.1 scan metadata assembly, default/legacy JSON export shapes, CLI
``--from-json`` replay, and tolerant deserialization of partial entries.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from software_inventory.cli import EXIT_OK, EXIT_RUNTIME, main, run_inventory
from software_inventory.exporters import export_json
from software_inventory.models import SoftwareEntry
from software_inventory.normalize import prepare_inventory_with_stats
from software_inventory.report import (
    SCHEMA_VERSION,
    build_report,
    build_scan_metadata,
    entry_from_dict,
    load_entries_from_payload,
)


def make_entry(**overrides) -> SoftwareEntry:
    """
    Build a ``SoftwareEntry`` fixture for report/envelope tests.

    Args:
        **overrides: Field overrides applied on top of the Sample App template.

    Returns:
        SoftwareEntry: Immutable test row.
    """
    data = {
        "name": "Sample App",
        "version": "1.0.0",
        "publisher": "Sample Co",
        "install_date": "2026-07-17",
        "install_location": r"C:\Program Files\Sample",
        "estimated_size_kb": 2048,
        "scope": "machine",
        "architecture": "64-bit",
        "uninstall_string": "uninstall.exe",
        "quiet_uninstall_string": None,
        "registry_path": r"HKEY_LOCAL_MACHINE\SOFTWARE\...\Sample",
        "release_type": None,
        "system_component": False,
    }
    data.update(overrides)
    return SoftwareEntry(**data)


class ReportEnvelopeTests(unittest.TestCase):
    """v1.1 envelope assembly, export defaults, and ``--from-json`` CLI."""

    def test_build_report_schema_and_counts(self) -> None:
        entries = [
            make_entry(name="App A"),
            make_entry(
                name="System Thing",
                system_component=True,
                registry_path="sys",
            ),
            make_entry(name="KB1234567", registry_path="kb"),
            make_entry(
                name="App A",
                version="1.0.0",
                publisher="Sample Co",
                install_location=r"C:\Program Files\Sample",
                estimated_size_kb=None,
                uninstall_string=None,
                registry_path="dup-sparse",
            ),
        ]
        prepared, stats = prepare_inventory_with_stats(entries)
        self.assertEqual(stats.raw_entry_count, 4)
        self.assertEqual(stats.deduplicated_count, 1)
        self.assertEqual(stats.filtered_system_component_count, 1)
        self.assertEqual(stats.filtered_update_count, 1)
        self.assertEqual(stats.result_count, 1)

        started = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)
        completed = datetime(2026, 7, 17, 6, 0, 1, tzinfo=timezone.utc)
        scan = build_scan_metadata(
            started_at=started,
            completed_at=completed,
            hostname="test-host",
            platform="Windows-10",
            collector_sources=["HKLM\\Uninstall"],
            stats=stats,
        )
        report = build_report(prepared, scan)
        payload = report.to_dict()

        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["scan"]["hostname"], "test-host")
        self.assertEqual(payload["scan"]["started_at"], "2026-07-17T06:00:00Z")
        self.assertEqual(payload["scan"]["completed_at"], "2026-07-17T06:00:01Z")
        self.assertEqual(payload["scan"]["duration_ms"], 1000)
        self.assertEqual(payload["scan"]["raw_entry_count"], 4)
        self.assertEqual(payload["scan"]["deduplicated_count"], 1)
        self.assertEqual(payload["scan"]["filtered_system_component_count"], 1)
        self.assertEqual(payload["scan"]["filtered_update_count"], 1)
        self.assertEqual(payload["scan"]["result_count"], 1)
        self.assertEqual(len(payload["software"]), 1)
        self.assertNotIn("username", payload["scan"])

    def test_export_json_envelope_by_default(self) -> None:
        entries = [make_entry()]
        prepared, stats = prepare_inventory_with_stats(entries)
        started = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)
        scan = build_scan_metadata(
            started_at=started,
            completed_at=started,
            hostname="host",
            platform="win",
            collector_sources=[],
            stats=stats,
        )
        report = build_report(prepared, scan)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out" / "report.json"
            export_json(prepared, output=path, pretty=True, report=report)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "1.2")
            self.assertIn("software", payload)
            self.assertEqual(payload["software"][0]["name"], "Sample App")
            self.assertEqual(payload["software"][0]["source"], "registry")

    def test_legacy_json_array(self) -> None:
        entries = [make_entry(name="レガシー")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            export_json(entries, output=path, legacy_json=True, pretty=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, list)
            self.assertEqual(payload[0]["name"], "レガシー")

    def test_run_inventory_writes_envelope(self) -> None:
        entries = [make_entry(name="CLI App")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "scan.json"
            code = run_inventory(
                format_name="json",
                output=path,
                pretty=True,
                entries=entries,
                hostname="fixture-host",
                platform_name="Windows-Test",
                collector_sources=["fixture-source"],
            )
            self.assertEqual(code, EXIT_OK)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "1.2")
            self.assertEqual(payload["scan"]["hostname"], "fixture-host")
            self.assertEqual(payload["scan"]["collector_sources"], ["fixture-source"])
            self.assertEqual(payload["software"][0]["name"], "CLI App")

    def test_run_inventory_legacy_json(self) -> None:
        entries = [make_entry()]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            code = run_inventory(
                format_name="json",
                output=path,
                legacy_json=True,
                entries=entries,
            )
            self.assertEqual(code, EXIT_OK)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, list)

    def test_main_from_json_replay(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "cli" / "legacy.json"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            code = main(
                [
                    "--from-json",
                    str(fixture),
                    "--format",
                    "json",
                    "--legacy-json",
                    "--output",
                    str(out),
                ]
            )
            self.assertEqual(code, EXIT_OK)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, list)
            self.assertEqual(payload[0]["name"], "Legacy App")

    def test_main_from_json_missing_file(self) -> None:
        code = main(["--from-json", str(Path("no-such-snapshot.json"))])
        self.assertEqual(code, EXIT_RUNTIME)


class PayloadLoadingTests(unittest.TestCase):
    """Deserialization of legacy arrays and versioned report objects."""

    def test_load_legacy_array(self) -> None:
        entries = load_entries_from_payload([make_entry().to_dict()])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Sample App")

    def test_load_envelope(self) -> None:
        payload = {
            "schema_version": "1.1",
            "scan": {},
            "software": [make_entry(name="Env App").to_dict()],
        }
        entries = load_entries_from_payload(payload)
        self.assertEqual(entries[0].name, "Env App")

    def test_v1_1_envelope_defaults_source_to_registry(self) -> None:
        row = make_entry(name="Old App").to_dict()
        row.pop("source")
        entries = load_entries_from_payload(
            {"schema_version": "1.1", "scan": {}, "software": [row]}
        )
        self.assertEqual(entries[0].source, "registry")

    def test_v1_2_envelope_keeps_appx_source(self) -> None:
        row = make_entry(name="Store App", source="appx").to_dict()
        entries = load_entries_from_payload(
            {"schema_version": "1.2", "scan": {}, "software": [row]}
        )
        self.assertEqual(entries[0].source, "appx")

    def test_unsupported_schema(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            load_entries_from_payload({"schema_version": "9.9", "software": []})
        self.assertIn("unsupported schema_version", str(ctx.exception))

    def test_invalid_payload_type(self) -> None:
        with self.assertRaises(ValueError):
            load_entries_from_payload("nope")

    def test_missing_name(self) -> None:
        with self.assertRaises(ValueError):
            entry_from_dict({"version": "1.0"})

    def test_missing_fields_are_tolerated(self) -> None:
        entry = entry_from_dict({"name": "Minimal"})
        self.assertIsNone(entry.version)
        self.assertIsNone(entry.publisher)
        self.assertFalse(entry.system_component)


if __name__ == "__main__":
    unittest.main()
