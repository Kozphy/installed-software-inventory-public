"""
Unit tests for inventory snapshot comparison.

Covers identity keys (version excluded so upgrades are changes), compare
classification, file loading of legacy/envelope JSON, and CLI ``diff`` exit
codes — all fixture-driven with no live Registry access.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from software_inventory.cli import EXIT_OK, EXIT_RUNTIME, main, run_diff
from software_inventory.diff import (
    compare_inventories,
    format_diff_table,
    identity_key,
    load_inventory_file,
)
from software_inventory.models import SoftwareEntry


def make_entry(**overrides) -> SoftwareEntry:
    """
    Build a ``SoftwareEntry`` fixture with sensible defaults for diff tests.

    Args:
        **overrides: Field overrides applied on top of the Shared App template.

    Returns:
        SoftwareEntry: Immutable test row.
    """
    data = {
        "name": "Shared App",
        "version": "1.0.0",
        "publisher": "Contoso",
        "install_date": "2026-01-01",
        "install_location": r"C:\Program Files\Shared",
        "estimated_size_kb": 1000,
        "scope": "machine",
        "architecture": "64-bit",
        "uninstall_string": "uninstall.exe",
        "quiet_uninstall_string": None,
        "registry_path": r"HKEY_LOCAL_MACHINE\SOFTWARE\...\Shared",
        "release_type": None,
        "system_component": False,
    }
    data.update(overrides)
    return SoftwareEntry(**data)


class DiffIdentityTests(unittest.TestCase):
    """Identity-key normalization used to match apps across snapshots."""

    def test_identity_excludes_version(self) -> None:
        a = make_entry(version="1.0")
        b = make_entry(version="2.0")
        self.assertEqual(identity_key(a), identity_key(b))

    def test_identity_normalizes_case_and_slashes(self) -> None:
        a = make_entry(
            name="Foo",
            publisher="Bar",
            install_location=r"C:\Program Files\Foo\\",
        )
        b = make_entry(
            name="foo",
            publisher="BAR",
            install_location=r"C:\Program Files\Foo",
        )
        self.assertEqual(identity_key(a), identity_key(b))


class DiffCompareTests(unittest.TestCase):
    """Added/removed/changed classification and table formatting."""

    def test_added_removed_changed(self) -> None:
        old = [
            make_entry(name="Keep", version="1.0", registry_path="keep"),
            make_entry(name="Gone", version="1.0", registry_path="gone"),
            make_entry(name="Upgrade", version="1.0", registry_path="up"),
        ]
        new = [
            make_entry(name="Keep", version="1.0", registry_path="keep"),
            make_entry(name="New", version="3.0", registry_path="new"),
            make_entry(
                name="Upgrade",
                version="2.0",
                install_date="2026-07-17",
                registry_path="up",
            ),
        ]
        result = compare_inventories(old, new)
        self.assertEqual(result.summary.added, 1)
        self.assertEqual(result.summary.removed, 1)
        self.assertEqual(result.summary.changed, 1)
        self.assertEqual(result.summary.unchanged, 1)
        self.assertEqual(result.added[0].name, "New")
        self.assertEqual(result.removed[0].name, "Gone")
        self.assertEqual(result.changed[0].name, "Upgrade")
        self.assertEqual(result.changed[0].old_version, "1.0")
        self.assertEqual(result.changed[0].new_version, "2.0")
        changed_fields = {c.field for c in result.changed[0].changes}
        self.assertIn("version", changed_fields)
        self.assertIn("install_date", changed_fields)

    def test_stable_ordering(self) -> None:
        old = [
            make_entry(name="Zed", registry_path="z"),
            make_entry(name="Alpha", registry_path="a"),
        ]
        new = [
            make_entry(name="Zed", version="9", registry_path="z"),
            make_entry(name="Beta", registry_path="b"),
            make_entry(name="Alpha", version="2", registry_path="a"),
        ]
        result = compare_inventories(old, new)
        self.assertEqual([e.name for e in result.added], ["Beta"])
        self.assertEqual([e.name for e in result.changed], ["Alpha", "Zed"])

    def test_non_ascii_names(self) -> None:
        old = [make_entry(name="日本語アプリ", version="1.0")]
        new = [make_entry(name="日本語アプリ", version="1.1")]
        result = compare_inventories(old, new)
        self.assertEqual(result.summary.changed, 1)
        self.assertEqual(result.changed[0].name, "日本語アプリ")

    def test_diff_table_contains_summary(self) -> None:
        result = compare_inventories(
            [make_entry(name="A")],
            [make_entry(name="B", registry_path="b")],
        )
        text = format_diff_table(result)
        self.assertIn("Added:", text)
        self.assertIn("Removed:", text)
        self.assertIn("+ B", text)
        self.assertIn("- A", text)


class DiffFileLoadingTests(unittest.TestCase):
    """JSON snapshot loading for legacy arrays and v1.1 envelopes."""

    def test_load_legacy_and_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "old.json"
            envelope = Path(tmp) / "new.json"
            legacy.write_text(
                json.dumps([make_entry(name="Legacy").to_dict()], ensure_ascii=False),
                encoding="utf-8",
            )
            envelope.write_text(
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "scan": {"hostname": "x"},
                        "software": [make_entry(name="Envelope").to_dict()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_inventory_file(legacy)[0].name, "Legacy")
            self.assertEqual(load_inventory_file(envelope)[0].name, "Envelope")

    def test_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_inventory_file(path)
            self.assertIn("invalid JSON", str(ctx.exception))

    def test_unsupported_schema_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future.json"
            path.write_text(
                json.dumps({"schema_version": "99.0", "software": []}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_inventory_file(path)
            self.assertIn("unsupported schema_version", str(ctx.exception))

    def test_utf8_bom_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.json"
            payload = json.dumps([make_entry(name="BOM App").to_dict()])
            path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
            entries = load_inventory_file(path)
            self.assertEqual(entries[0].name, "BOM App")

    def test_missing_file(self) -> None:
        with self.assertRaises(ValueError):
            load_inventory_file(Path("definitely-missing-inventory-xyz.json"))


class DiffCliTests(unittest.TestCase):
    """CLI ``run_diff`` / ``main(['diff', ...])`` exit-code contract."""

    def test_run_diff_json_output_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.json"
            new = Path(tmp) / "new.json"
            out = Path(tmp) / "reports" / "diff.json"
            old.write_text(
                json.dumps([make_entry(name="A", version="1").to_dict()]),
                encoding="utf-8",
            )
            new.write_text(
                json.dumps([make_entry(name="A", version="2").to_dict()]),
                encoding="utf-8",
            )
            code = run_diff(
                old_path=old,
                new_path=new,
                format_name="json",
                output=out,
                pretty=True,
            )
            self.assertEqual(code, EXIT_OK)
            self.assertTrue(out.is_file())
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["changed"], 1)

            bad = Path(tmp) / "bad.json"
            bad.write_text("[]", encoding="utf-8")
            missing = Path(tmp) / "nope.json"
            code = run_diff(old_path=old, new_path=missing)
            self.assertEqual(code, EXIT_RUNTIME)

    def test_main_diff_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.json"
            new = Path(tmp) / "new.json"
            out = Path(tmp) / "diff.txt"
            old.write_text("[]", encoding="utf-8")
            new.write_text(
                json.dumps([make_entry(name="OnlyNew").to_dict()]),
                encoding="utf-8",
            )
            code = main(
                [
                    "diff",
                    str(old),
                    str(new),
                    "--format",
                    "table",
                    "--output",
                    str(out),
                ]
            )
            self.assertEqual(code, EXIT_OK)
            text = out.read_text(encoding="utf-8")
            self.assertIn("OnlyNew", text)
            self.assertIn("Added:", text)


if __name__ == "__main__":
    unittest.main()
