"""
Unit tests for the winget reinstall bridge.

Covers table parsing, fixture loading, name matching, import JSON shape,
and unmatched Markdown — all offline (no live ``winget`` / Registry).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from software_inventory.exporters import export_winget
from software_inventory.models import SoftwareEntry
from software_inventory.winget_bridge import (
    WingetPackage,
    build_winget_import_document,
    default_unmatched_path,
    format_unmatched_markdown,
    load_winget_packages_json,
    match_inventory_to_winget,
    normalize_match_key,
    parse_winget_list_table,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cli"
WINGET_LIST_FIXTURE = FIXTURE_DIR / "winget-list.json"


def make_entry(**overrides) -> SoftwareEntry:
    """Build a ``SoftwareEntry`` fixture for bridge tests."""
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
        "quiet_uninstall_string": None,
        "registry_path": r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Sample",
        "release_type": None,
        "system_component": False,
    }
    data.update(overrides)
    return SoftwareEntry(**data)


SAMPLE_LIST = """\
Name                                                         Id                                                                                    Version                         Available           Source
--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Keep App                                                     Contoso.KeepApp                                                                       1.0.0                                              winget
Canva                                                        ARP\\User\\X64\\abc                                                                     1.2.3                                               
CapCut                                                       ByteDance.CapCut                                                                      9.3.0                           9.4.0              winget
"""


class WingetBridgeTests(unittest.TestCase):
    """Matching, parsing, and import document construction."""

    def test_normalize_strips_trailing_version(self) -> None:
        self.assertEqual(normalize_match_key("CapCut 9.3.0"), "capcut")
        self.assertEqual(normalize_match_key("  Keep App  "), "keep app")

    def test_parse_winget_list_table(self) -> None:
        packages = parse_winget_list_table(SAMPLE_LIST)
        self.assertEqual(len(packages), 3)
        self.assertEqual(packages[0].package_id, "Contoso.KeepApp")
        self.assertTrue(packages[0].is_importable())
        self.assertFalse(packages[1].is_importable())
        self.assertEqual(packages[2].source, "winget")

    def test_load_fixture_json(self) -> None:
        packages = load_winget_packages_json(WINGET_LIST_FIXTURE)
        self.assertGreaterEqual(len(packages), 3)
        ids = {pkg.package_id for pkg in packages}
        self.assertIn("Contoso.KeepApp", ids)

    def test_match_exact_and_versioned_name(self) -> None:
        packages = load_winget_packages_json(WINGET_LIST_FIXTURE)
        entries = [
            make_entry(name="Keep App", version="1.0.0", publisher="Contoso"),
            make_entry(name="Upgrade App", version="2.0.0", publisher="Contoso"),
            make_entry(name="New Tool", version="0.9.0", publisher="Vendor"),
            make_entry(name="Canva", version="1.124.1", publisher="Canva"),
        ]
        result = match_inventory_to_winget(entries, packages)
        matched_ids = {item.package.package_id for item in result.matched}
        self.assertEqual(matched_ids, {"Contoso.KeepApp", "Contoso.UpgradeApp"})
        unmatched_names = {entry.name for entry in result.unmatched}
        self.assertEqual(unmatched_names, {"New Tool", "Canva"})

    def test_import_document_shape(self) -> None:
        packages = [
            WingetPackage("Keep App", "Contoso.KeepApp", "1.0.0", "winget"),
        ]
        entries = [make_entry(name="Keep App", version="1.0.0")]
        result = match_inventory_to_winget(entries, packages)
        document = build_winget_import_document(
            result,
            include_versions=True,
            creation_date=datetime(2026, 9, 24, 5, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(document["$schema"], "https://aka.ms/winget-packages.schema.2.0.json")
        packages_out = document["Sources"][0]["Packages"]
        self.assertEqual(packages_out[0]["PackageIdentifier"], "Contoso.KeepApp")
        self.assertEqual(packages_out[0]["Version"], "1.0.0")
        self.assertIn("SourceDetails", document["Sources"][0])

    def test_empty_matches_yield_empty_sources(self) -> None:
        result = match_inventory_to_winget(
            [make_entry(name="Unknown")],
            [WingetPackage("Other", "Other.Id", source="winget")],
        )
        document = build_winget_import_document(result)
        self.assertEqual(document["Sources"], [])

    def test_unmatched_markdown_and_export(self) -> None:
        packages = load_winget_packages_json(WINGET_LIST_FIXTURE)
        entries = [
            make_entry(name="Keep App"),
            make_entry(name="Manual Only", publisher="Vendor", version="9"),
        ]
        result = match_inventory_to_winget(entries, packages)
        text = format_unmatched_markdown(result.unmatched, matched_count=1)
        self.assertIn("Manual Only", text)
        self.assertIn("[ ]", text)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "reinstall" / "packages.json"
            unmatched = default_unmatched_path(out)
            self.assertEqual(unmatched.name, "packages.unmatched.md")
            export_winget(result, output=out, unmatched_output=unmatched)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["Sources"][0]["Packages"][0]["PackageIdentifier"],
                "Contoso.KeepApp",
            )
            checklist = unmatched.read_text(encoding="utf-8")
            self.assertIn("Manual Only", checklist)


if __name__ == "__main__":
    unittest.main()
