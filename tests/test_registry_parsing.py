"""
Unit tests for Registry value → SoftwareEntry conversion (no live Registry).

Validates the collector's mapping of Uninstall value maps into the shared
``SoftwareEntry`` model, including missing/invalid fields.
"""

from __future__ import annotations

import unittest

from software_inventory.collectors.windows_registry import _entry_from_values


class RegistryValueParsingTests(unittest.TestCase):
    """``_entry_from_values`` mapping from Uninstall value maps."""

    def test_missing_values_do_not_crash(self) -> None:
        entry = _entry_from_values(
            {
                "DisplayName": "Partial App",
                "DisplayVersion": None,
                "Publisher": None,
                "InstallDate": "bogus",
                "InstallLocation": None,
                "EstimatedSize": None,
                "UninstallString": None,
                "QuietUninstallString": None,
                "ReleaseType": None,
                "SystemComponent": None,
            },
            registry_path=r"HKEY_LOCAL_MACHINE\SOFTWARE\...\Partial",
            scope="machine",
            architecture="64-bit",
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.name, "Partial App")
        self.assertIsNone(entry.version)
        self.assertIsNone(entry.publisher)
        self.assertIsNone(entry.install_date)
        self.assertIsNone(entry.estimated_size_kb)
        self.assertFalse(entry.system_component)

    def test_skips_entries_without_display_name(self) -> None:
        self.assertIsNone(
            _entry_from_values(
                {"DisplayName": None},
                registry_path="x",
                scope="machine",
                architecture="unknown",
            )
        )
        self.assertIsNone(
            _entry_from_values(
                {"DisplayName": "   "},
                registry_path="x",
                scope="machine",
                architecture="unknown",
            )
        )

    def test_parses_common_registry_shapes(self) -> None:
        entry = _entry_from_values(
            {
                "DisplayName": "Contoso Suite",
                "DisplayVersion": "9.8.7",
                "Publisher": "Contoso",
                "InstallDate": "20260717",
                "InstallLocation": r"C:\Program Files\Contoso",
                "EstimatedSize": 2048,
                "UninstallString": "uninstall.exe",
                "QuietUninstallString": "uninstall.exe /quiet",
                "ReleaseType": None,
                "SystemComponent": 0,
            },
            registry_path=r"HKEY_LOCAL_MACHINE\SOFTWARE\...\Contoso",
            scope="machine",
            architecture="32-bit",
        )
        assert entry is not None
        self.assertEqual(entry.install_date, "2026-07-17")
        self.assertEqual(entry.estimated_size_kb, 2048)
        self.assertEqual(entry.architecture, "32-bit")


if __name__ == "__main__":
    unittest.main()
