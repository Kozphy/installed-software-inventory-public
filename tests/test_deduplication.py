"""
Unit tests for deterministic software-entry deduplication.

Ensures overlapping Uninstall views collapse to one row and that the richer
metadata record wins — required for stable table/JSON/CSV exports.
"""

from __future__ import annotations

import unittest

from software_inventory.models import SoftwareEntry
from software_inventory.normalize import deduplicate_entries, deduplication_key


def make_entry(**overrides) -> SoftwareEntry:
    """
    Build a ``SoftwareEntry`` fixture for deduplication tests.

    Args:
        **overrides: Field overrides applied on top of the Shared App template.

    Returns:
        SoftwareEntry: Immutable test row.
    """
    data = {
        "name": "Shared App",
        "version": "2.0",
        "publisher": "Contoso",
        "install_date": None,
        "install_location": r"C:\Program Files\Shared",
        "estimated_size_kb": None,
        "scope": "machine",
        "architecture": "64-bit",
        "uninstall_string": None,
        "quiet_uninstall_string": None,
        "registry_path": r"HKEY_LOCAL_MACHINE\SOFTWARE\...\Shared",
        "release_type": None,
        "system_component": False,
    }
    data.update(overrides)
    return SoftwareEntry(**data)


class DeduplicationTests(unittest.TestCase):
    """Key equality, richness preference, and deterministic tie-breaking."""

    def test_identical_keys_collapse(self) -> None:
        """Rows with the same dedupe key collapse to one."""
        sparse = make_entry(
            registry_path=r"HKEY_LOCAL_MACHINE\A\Sparse",
            install_date=None,
            uninstall_string=None,
            estimated_size_kb=None,
        )
        rich = make_entry(
            registry_path=r"HKEY_LOCAL_MACHINE\B\Rich",
            install_date="2026-07-17",
            uninstall_string="uninstall.exe",
            quiet_uninstall_string="uninstall.exe /S",
            estimated_size_kb=2048,
            publisher="Contoso",
        )
        result = deduplicate_entries([sparse, rich])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].registry_path, rich.registry_path)
        self.assertEqual(result[0].install_date, "2026-07-17")
        self.assertGreater(result[0].completeness_score(), sparse.completeness_score())

    def test_prefers_richer_metadata_regardless_of_order(self) -> None:
        """The row with more populated fields wins in either input order."""
        sparse = make_entry(registry_path="path-a", estimated_size_kb=None)
        rich = make_entry(
            registry_path="path-b",
            estimated_size_kb=4096,
            uninstall_string="u.exe",
            install_date="2026-01-01",
        )
        forward = deduplicate_entries([sparse, rich])
        reverse = deduplicate_entries([rich, sparse])
        self.assertEqual(forward[0].registry_path, "path-b")
        self.assertEqual(reverse[0].registry_path, "path-b")

    def test_different_keys_remain_separate(self) -> None:
        """Different names or versions stay separate rows."""
        a = make_entry(name="App A", version="1.0", registry_path="a")
        b = make_entry(name="App B", version="1.0", registry_path="b")
        c = make_entry(name="App A", version="2.0", registry_path="c")
        result = deduplicate_entries([a, b, c])
        self.assertEqual(len(result), 3)

    def test_key_normalization_is_case_insensitive(self) -> None:
        """Case and trailing-slash differences share one dedupe key."""
        left = make_entry(
            name="Foo",
            version="1.0",
            publisher="Bar",
            install_location=r"C:\Program Files\Foo\\",
            registry_path="left",
        )
        right = make_entry(
            name="foo",
            version="1.0",
            publisher="BAR",
            install_location=r"C:\Program Files\Foo",
            registry_path="right",
            install_date="2026-07-01",
        )
        self.assertEqual(deduplication_key(left), deduplication_key(right))
        result = deduplicate_entries([left, right])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].install_date, "2026-07-01")

    def test_missing_values_do_not_crash_dedup(self) -> None:
        """Rows with missing optional fields dedupe without errors."""
        entry = make_entry(
            version=None,
            publisher=None,
            install_location=None,
            install_date=None,
        )
        result = deduplicate_entries([entry, entry])
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
