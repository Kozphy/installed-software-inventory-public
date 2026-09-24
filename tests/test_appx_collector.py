"""
Unit tests for the Appx/MSIX collector and its integration points.

Never launches PowerShell: the subprocess runner is injected, so the suite
runs on any OS. Covers JSON → SoftwareEntry mapping, the enumeration script's
read-only contract, collector orchestration for ``--source``, the prepare-time
Registry-over-Appx merge policy, and Appx identity in snapshot diffs.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import subprocess
import unittest
from typing import Any

from software_inventory.collectors import collect_inventory
from software_inventory.collectors.windows_appx import (
    APPX_QUERY_SCRIPT,
    APPX_SOURCE_LABEL,
    architecture_label,
    collect_from_appx,
    entry_from_appx_package,
    parse_appx_packages,
    publisher_from_subject,
    run_appx_query,
)
from software_inventory.diff import compare_inventories, identity_key
from software_inventory.exporters import export_csv
from software_inventory.models import CSV_FIELDNAMES, SoftwareEntry
from software_inventory.normalize import (
    merge_cross_source_duplicates,
    prepare_inventory_with_stats,
)

TERMINAL_PACKAGE: dict[str, Any] = {
    "name": "Microsoft.WindowsTerminal",
    "family": "Microsoft.WindowsTerminal_8wekyb3d8bbwe",
    "full_name": "Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe",
    "display_name": "Windows Terminal",
    "publisher_display_name": "Microsoft Corporation",
    "publisher": "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US",
    "version": "1.24.11911.0",
    "architecture": "X64",
    "install_location": r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe",
    "installed_date": "2026-07-23T18:36:36.1877983+08:00",
    "is_framework": False,
    "is_resource": False,
    "is_bundle": False,
    "signature_kind": "Store",
}


def make_package(**overrides: Any) -> dict[str, Any]:
    """
    Build one enumerated-package dict shaped like the PowerShell output.

    Args:
        **overrides: Keys replaced on top of the Windows Terminal template.

    Returns:
        dict[str, Any]: Package object.
    """
    data = dict(TERMINAL_PACKAGE)
    data.update(overrides)
    return data


def make_entry(**overrides: Any) -> SoftwareEntry:
    """
    Build a Registry-style ``SoftwareEntry`` fixture.

    Args:
        **overrides: Field overrides applied on top of the template.

    Returns:
        SoftwareEntry: Immutable test row.
    """
    data: dict[str, Any] = {
        "name": "Sample App",
        "version": "1.0.0",
        "publisher": "Sample Co",
        "install_date": None,
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


def appx_entry(**overrides: Any) -> SoftwareEntry:
    """
    Map a package template straight to a ``SoftwareEntry``.

    Args:
        **overrides: Package-level overrides.

    Returns:
        SoftwareEntry: Appx row (asserted non-None).
    """
    entry = entry_from_appx_package(make_package(**overrides))
    assert entry is not None
    return entry


class FakeRunner:
    """Records the argv it receives and returns a canned CompletedProcess."""

    def __init__(self, *, stdout: bytes = b"[]", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class AppxMappingTests(unittest.TestCase):
    """``entry_from_appx_package`` field mapping."""

    def test_maps_store_package(self) -> None:
        entry = appx_entry()
        self.assertEqual(entry.name, "Windows Terminal")
        self.assertEqual(entry.version, "1.24.11911.0")
        self.assertEqual(entry.publisher, "Microsoft Corporation")
        self.assertEqual(entry.install_date, "2026-07-23")
        self.assertEqual(entry.architecture, "64-bit")
        self.assertEqual(entry.scope, "current_user")
        self.assertEqual(entry.registry_path, "appx:Microsoft.WindowsTerminal_8wekyb3d8bbwe")
        self.assertEqual(entry.source, "appx")
        self.assertIsNone(entry.uninstall_string)
        self.assertIsNone(entry.estimated_size_kb)
        self.assertFalse(entry.system_component)

    def test_resource_packages_are_dropped(self) -> None:
        self.assertIsNone(entry_from_appx_package(make_package(is_resource=True)))

    def test_blank_or_unresolved_display_name_falls_back_to_package_name(self) -> None:
        self.assertEqual(appx_entry(display_name="").name, "Microsoft.WindowsTerminal")
        self.assertEqual(
            appx_entry(display_name="ms-resource:AppName").name,
            "Microsoft.WindowsTerminal",
        )

    def test_frameworks_and_os_packages_are_system_components(self) -> None:
        self.assertTrue(appx_entry(is_framework=True).system_component)
        self.assertTrue(appx_entry(signature_kind="System").system_component)
        self.assertFalse(appx_entry(signature_kind="Developer").system_component)

    def test_publisher_falls_back_to_signing_subject(self) -> None:
        entry = appx_entry(publisher_display_name=None)
        self.assertEqual(entry.publisher, "Microsoft Corporation")

    def test_missing_identity_is_dropped(self) -> None:
        self.assertIsNone(entry_from_appx_package({"display_name": "Ghost"}))

    def test_invalid_installed_date_becomes_none(self) -> None:
        self.assertIsNone(appx_entry(installed_date="1601-01-01T00:00:00Z").install_date)
        self.assertIsNone(appx_entry(installed_date=None).install_date)

    def test_publisher_from_subject(self) -> None:
        self.assertEqual(
            publisher_from_subject("CN=Microsoft Windows, O=Microsoft Corporation, C=US"),
            "Microsoft Corporation",
        )
        self.assertEqual(publisher_from_subject("CN=Contoso Dev"), "Contoso Dev")
        self.assertIsNone(publisher_from_subject(None))

    def test_architecture_labels(self) -> None:
        self.assertEqual(architecture_label("X64"), "64-bit")
        self.assertEqual(architecture_label("X86"), "32-bit")
        self.assertEqual(architecture_label("Arm64"), "arm64")
        self.assertEqual(architecture_label("Neutral"), "neutral")
        self.assertEqual(architecture_label("Mystery"), "unknown")


class AppxParsingTests(unittest.TestCase):
    """``parse_appx_packages`` output shapes."""

    def test_array(self) -> None:
        self.assertEqual(len(parse_appx_packages(json.dumps([TERMINAL_PACKAGE, 3]))), 1)

    def test_single_object(self) -> None:
        self.assertEqual(len(parse_appx_packages(json.dumps(TERMINAL_PACKAGE))), 1)

    def test_blank_output(self) -> None:
        self.assertEqual(parse_appx_packages("  \r\n"), [])

    def test_invalid_json(self) -> None:
        with self.assertRaises(ValueError):
            parse_appx_packages("Get-AppxPackage : access denied")


class AppxQueryTests(unittest.TestCase):
    """Subprocess contract with an injected runner (no PowerShell launched)."""

    def test_script_is_enumeration_only(self) -> None:
        self.assertIn("FindPackagesForUser", APPX_QUERY_SCRIPT)
        for forbidden in ("RemovePackage", "Remove-Appx", "Add-Appx", "AddPackage", "RegisterPackage"):
            self.assertNotIn(forbidden, APPX_QUERY_SCRIPT)

    def test_collect_uses_encoded_command(self) -> None:
        payload = json.dumps([TERMINAL_PACKAGE, make_package(is_resource=True)])
        runner = FakeRunner(stdout=payload.encode("utf-8"))
        entries = collect_from_appx(runner=runner)
        self.assertEqual([entry.name for entry in entries], ["Windows Terminal"])
        argv = runner.calls[0]
        self.assertIn("-NoProfile", argv)
        self.assertIn("-NonInteractive", argv)
        encoded = argv[argv.index("-EncodedCommand") + 1]
        self.assertEqual(base64.b64decode(encoded).decode("utf-16-le"), APPX_QUERY_SCRIPT)

    def test_script_escapes_non_ascii_output(self) -> None:
        self.assertIn("[^\\x00-\\x7F]", APPX_QUERY_SCRIPT)
        entries = collect_from_appx(
            runner=FakeRunner(stdout=b'[{"name":"A","family":"A_x","display_name":"Authenticator App \\u2013 2FA"}]')
        )
        self.assertEqual(entries[0].name, "Authenticator App – 2FA")

    def test_utf8_output_round_trips(self) -> None:
        payload = json.dumps([make_package(display_name="Authenticator App – 2FA")], ensure_ascii=False)
        runner = FakeRunner(stdout=b"\xef\xbb\xbf" + payload.encode("utf-8"))
        entries = collect_from_appx(runner=runner)
        self.assertEqual(entries[0].name, "Authenticator App – 2FA")

    def test_non_zero_exit_raises_with_clixml_summary(self) -> None:
        stderr = (
            b'#< CLIXML\r\n<Objs Version="1.1.0.1"><S S="Error">Unable to find type'
            b"_x000D__x000A_</S></Objs>"
        )
        runner = FakeRunner(stdout=b"", stderr=stderr, returncode=1)
        with self.assertRaises(RuntimeError) as ctx:
            run_appx_query(runner=runner, executable="powershell.exe")
        self.assertIn("exit 1", str(ctx.exception))
        self.assertIn("Unable to find type", str(ctx.exception))
        self.assertNotIn("CLIXML", str(ctx.exception))

    def test_timeout_raises_runtime_error(self) -> None:
        def slow_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

        with self.assertRaises(RuntimeError) as ctx:
            run_appx_query(runner=slow_runner, executable="powershell.exe", timeout=5)
        self.assertIn("timed out", str(ctx.exception))

    def test_skip_env_blocks_live_appx_scan(self) -> None:
        previous = os.environ.get("SOFTWARE_INVENTORY_SKIP_LIVE_SCAN")
        os.environ["SOFTWARE_INVENTORY_SKIP_LIVE_SCAN"] = "1"
        try:
            with self.assertRaises(RuntimeError) as ctx:
                collect_from_appx()
            self.assertIn("SOFTWARE_INVENTORY_SKIP_LIVE_SCAN", str(ctx.exception))
        finally:
            if previous is None:
                os.environ.pop("SOFTWARE_INVENTORY_SKIP_LIVE_SCAN", None)
            else:
                os.environ["SOFTWARE_INVENTORY_SKIP_LIVE_SCAN"] = previous


class CollectInventoryTests(unittest.TestCase):
    """``collect_inventory`` orchestration for ``--source``."""

    def setUp(self) -> None:
        """Provide one Registry row and one Appx row from stub collectors."""
        self.registry_rows = [make_entry(name="Reg App")]
        self.appx_rows = [appx_entry()]

    def _fail(self) -> list[SoftwareEntry]:
        raise RuntimeError("Appx package query failed (exit 1): blocked by policy")

    def test_all_combines_sources(self) -> None:
        entries, labels = collect_inventory(
            "all",
            registry_collector=lambda: self.registry_rows,
            appx_collector=lambda: self.appx_rows,
        )
        self.assertEqual([e.source for e in entries], ["registry", "appx"])
        self.assertEqual(labels[-1], APPX_SOURCE_LABEL)
        self.assertEqual(len(labels), 5)

    def test_all_survives_appx_failure(self) -> None:
        with self.assertLogs("software_inventory.collectors", level="WARNING") as logs:
            entries, labels = collect_inventory(
                "all",
                registry_collector=lambda: self.registry_rows,
                appx_collector=self._fail,
            )
        self.assertEqual([e.name for e in entries], ["Reg App"])
        self.assertNotIn(APPX_SOURCE_LABEL, labels)
        self.assertIn("blocked by policy", logs.output[0])

    def test_appx_only_propagates_failure(self) -> None:
        with self.assertRaises(RuntimeError):
            collect_inventory(
                "appx",
                registry_collector=lambda: self.registry_rows,
                appx_collector=self._fail,
            )

    def test_registry_only_skips_appx(self) -> None:
        entries, labels = collect_inventory(
            "registry",
            registry_collector=lambda: self.registry_rows,
            appx_collector=self._fail,
        )
        self.assertEqual(len(entries), 1)
        self.assertNotIn(APPX_SOURCE_LABEL, labels)

    def test_unknown_source(self) -> None:
        with self.assertRaises(ValueError):
            collect_inventory("snap")


class CrossSourceMergeTests(unittest.TestCase):
    """Prepare-time policy: a Registry row shadows the same app from Appx."""

    def test_matching_appx_row_is_dropped(self) -> None:
        registry = make_entry(name="Windows Terminal", publisher="Microsoft Corporation")
        merged = merge_cross_source_duplicates([registry, appx_entry()])
        self.assertEqual(merged, [registry])

    def test_different_publisher_is_kept(self) -> None:
        registry = make_entry(name="Windows Terminal", publisher="Someone Else")
        merged = merge_cross_source_duplicates([registry, appx_entry()])
        self.assertEqual(len(merged), 2)

    def test_missing_publisher_counts_as_match(self) -> None:
        registry = make_entry(name="windows terminal", publisher=None)
        merged = merge_cross_source_duplicates([registry, appx_entry()])
        self.assertEqual(merged, [registry])

    def test_merge_is_counted_as_dedup(self) -> None:
        registry = make_entry(name="Windows Terminal", publisher="Microsoft Corporation")
        prepared, stats = prepare_inventory_with_stats([registry, appx_entry()])
        self.assertEqual(prepared, [registry])
        self.assertEqual(stats.deduplicated_count, 1)

    def test_frameworks_hidden_by_default(self) -> None:
        prepared, stats = prepare_inventory_with_stats([appx_entry(is_framework=True)])
        self.assertEqual(prepared, [])
        self.assertEqual(stats.filtered_system_component_count, 1)


class AppxDiffTests(unittest.TestCase):
    """Appx identity survives version bumps and relocalized names."""

    def test_upgrade_is_a_change_not_remove_add(self) -> None:
        old = appx_entry(
            version="1.23.0.0",
            install_location=r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_1.23.0.0_x64__8wekyb3d8bbwe",
        )
        new = appx_entry(display_name="Terminal")
        result = compare_inventories([old], [new])
        self.assertEqual(result.summary.added, 0)
        self.assertEqual(result.summary.removed, 0)
        self.assertEqual(result.summary.changed, 1)
        fields = {change.field for change in result.changed[0].changes}
        self.assertEqual(fields, {"name", "version", "install_location"})

    def test_architectures_stay_distinct(self) -> None:
        self.assertNotEqual(
            identity_key(appx_entry(architecture="X64")),
            identity_key(appx_entry(architecture="X86")),
        )

    def test_registry_identity_unchanged(self) -> None:
        entry = make_entry(name="App", publisher="Pub", install_location="C:\\X\\")
        self.assertEqual(identity_key(entry), ("app", "pub", "c:\\x"))


class SourceColumnTests(unittest.TestCase):
    """``source`` is appended to the CSV contract."""

    def test_csv_source_column_is_last(self) -> None:
        self.assertEqual(CSV_FIELDNAMES[-1], "source")
        buffer = io.StringIO()
        export_csv([appx_entry()], stream=buffer)
        rows = list(csv.DictReader(io.StringIO(buffer.getvalue())))
        self.assertEqual(rows[0]["source"], "appx")


if __name__ == "__main__":
    unittest.main()
