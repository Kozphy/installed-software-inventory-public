"""
Unit tests for normalization, filtering, and search helpers.

Exercises the prepare stage of the inventory pipeline (string/date/size
coercion, update detection, and ``--search`` matching) without touching the
live Registry.
"""

from __future__ import annotations

import unittest

from software_inventory.models import SoftwareEntry
from software_inventory.normalize import (
    filter_entries,
    format_size_human,
    is_windows_update,
    matches_search,
    normalize_estimated_size_kb,
    normalize_install_date,
    normalize_string,
    normalize_system_component,
    prepare_inventory,
)


def make_entry(**overrides) -> SoftwareEntry:
    """
    Build a ``SoftwareEntry`` with sensible defaults for normalize tests.

    Args:
        **overrides: Field overrides applied on top of the Example App template.

    Returns:
        SoftwareEntry: Immutable test row.
    """
    data = {
        "name": "Example App",
        "version": "1.0.0",
        "publisher": "Example Inc",
        "install_date": "2026-01-15",
        "install_location": r"C:\Program Files\Example",
        "estimated_size_kb": 1024,
        "scope": "machine",
        "architecture": "64-bit",
        "uninstall_string": r"C:\Program Files\Example\uninstall.exe",
        "quiet_uninstall_string": None,
        "registry_path": r"HKEY_LOCAL_MACHINE\SOFTWARE\...\Example",
        "release_type": None,
        "system_component": False,
    }
    data.update(overrides)
    return SoftwareEntry(**data)


class NormalizeStringTests(unittest.TestCase):
    """Registry string coercion at the collector→model boundary."""

    def test_missing_and_blank(self) -> None:
        """None, empty, and whitespace-only strings normalize to None."""
        self.assertIsNone(normalize_string(None))
        self.assertIsNone(normalize_string(""))
        self.assertIsNone(normalize_string("   "))

    def test_strips_whitespace(self) -> None:
        """Surrounding whitespace is stripped."""
        self.assertEqual(normalize_string("  hello  "), "hello")


class InstallDateTests(unittest.TestCase):
    """InstallDate normalization into ISO ``YYYY-MM-DD``."""

    def test_yyyymmdd(self) -> None:
        """``YYYYMMDD`` becomes ISO ``YYYY-MM-DD``."""
        self.assertEqual(normalize_install_date("20260717"), "2026-07-17")

    def test_iso_passthrough(self) -> None:
        """ISO dates pass through unchanged."""
        self.assertEqual(normalize_install_date("2026-07-17"), "2026-07-17")

    def test_invalid_dates_are_null(self) -> None:
        """Malformed or impossible dates normalize to None."""
        self.assertIsNone(normalize_install_date(None))
        self.assertIsNone(normalize_install_date(""))
        self.assertIsNone(normalize_install_date("not-a-date"))
        self.assertIsNone(normalize_install_date("20261301"))
        self.assertIsNone(normalize_install_date("20260230"))
        self.assertIsNone(normalize_install_date("123"))


class SizeTests(unittest.TestCase):
    """EstimatedSize parsing and human-readable table formatting."""

    def test_parse_estimated_size(self) -> None:
        """EstimatedSize accepts ints and digit strings; invalid or negative is None."""
        self.assertEqual(normalize_estimated_size_kb(850), 850)
        self.assertEqual(normalize_estimated_size_kb("1024"), 1024)
        self.assertIsNone(normalize_estimated_size_kb(None))
        self.assertIsNone(normalize_estimated_size_kb("abc"))
        self.assertIsNone(normalize_estimated_size_kb(-5))

    def test_human_readable_formatting(self) -> None:
        """Sizes render as KB, MB, or GB."""
        self.assertEqual(format_size_human(None), "")
        self.assertEqual(format_size_human(512), "512 KB")
        self.assertEqual(format_size_human(850 * 1024), "850 MB")
        self.assertEqual(format_size_human(int(1.4 * 1024 * 1024)), "1.4 GB")
        self.assertEqual(format_size_human(100 * 1024), "100 MB")


class SystemComponentTests(unittest.TestCase):
    """SystemComponent DWORD/string interpretation."""

    def test_system_component_flags(self) -> None:
        """SystemComponent is truthy only for 1 / ``"1"``."""
        self.assertFalse(normalize_system_component(None))
        self.assertFalse(normalize_system_component(0))
        self.assertTrue(normalize_system_component(1))
        self.assertTrue(normalize_system_component("1"))


class FilterTests(unittest.TestCase):
    """Default filters, update detection, search, and prepare_inventory."""

    def test_filters_missing_display_name(self) -> None:
        """Blank display names are filtered out."""
        entries = [
            make_entry(name=""),
            make_entry(name="   "),
            make_entry(name="Valid App"),
        ]
        # Empty names should not appear; prepare_inventory uses has_valid_display_name
        filtered = filter_entries(entries)
        self.assertEqual([e.name for e in filtered], ["Valid App"])

    def test_hides_system_components_by_default(self) -> None:
        """System components are hidden unless explicitly included."""
        entries = [
            make_entry(name="User App", system_component=False),
            make_entry(name="System Thing", system_component=True),
        ]
        hidden = filter_entries(entries)
        self.assertEqual([e.name for e in hidden], ["User App"])
        shown = filter_entries(entries, include_system_components=True)
        self.assertEqual({e.name for e in shown}, {"User App", "System Thing"})

    def test_hides_updates_by_default(self) -> None:
        """Windows updates and hotfixes are hidden unless explicitly included."""
        entries = [
            make_entry(name="Chrome"),
            make_entry(name="KB5025221"),
            make_entry(name="Security Update for Windows"),
            make_entry(name="Update for Microsoft Office"),
            make_entry(name="Normal Tool", release_type="Update"),
        ]
        hidden = filter_entries(entries)
        self.assertEqual([e.name for e in hidden], ["Chrome"])
        shown = filter_entries(entries, include_updates=True)
        self.assertEqual(len(shown), 5)

    def test_update_detection_helpers(self) -> None:
        """KB names and Hotfix release types count as updates; similar app names do not."""
        self.assertTrue(is_windows_update(make_entry(name="KB1234567")))
        self.assertTrue(is_windows_update(make_entry(name="App", release_type="Hotfix")))
        self.assertFalse(is_windows_update(make_entry(name="Update Helper Utility")))

    def test_case_insensitive_search(self) -> None:
        """``search`` matches name, publisher, or version case-insensitively."""
        entries = [
            make_entry(name="Visual Studio", publisher="Microsoft", version="17.0"),
            make_entry(name="Firefox", publisher="Mozilla", version="128.0"),
            make_entry(name="Other", publisher="Acme", version="ms-build-1"),
        ]
        by_name = filter_entries(entries, search="microsoft")
        self.assertEqual([e.name for e in by_name], ["Visual Studio"])

        by_version = filter_entries(entries, search="128.0")
        self.assertEqual([e.name for e in by_version], ["Firefox"])

        self.assertTrue(matches_search(entries[0], "visual"))
        self.assertTrue(matches_search(entries[0], "MICROSOFT"))
        self.assertFalse(matches_search(entries[1], "chrome"))

    def test_prepare_inventory_sorts_by_name(self) -> None:
        """``prepare_inventory`` returns rows sorted by name."""
        entries = [
            make_entry(name="Zebra", registry_path="z"),
            make_entry(name="alpha", registry_path="a"),
            make_entry(name="Beta", registry_path="b"),
        ]
        prepared = prepare_inventory(entries)
        self.assertEqual([e.name for e in prepared], ["alpha", "Beta", "Zebra"])


if __name__ == "__main__":
    unittest.main()
