"""
Process-level CLI harness tests (subprocess, fixtures, no live Registry).

Verifies that ``scripts/run_cli_harness.py`` loads correctly, that the full
contract suite passes, and that ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN`` blocks
``collect_from_registry`` so CI never touches Uninstall keys.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from software_inventory.collectors.windows_registry import collect_from_registry


def load_harness() -> ModuleType:
    """
    Load ``scripts/run_cli_harness.py`` as an importable module.

    The harness lives under ``scripts/`` (not a package), so tests load it via
    ``importlib`` instead of a normal package import.

    Returns:
        ModuleType: Loaded harness module.

    Raises:
        RuntimeError: When the module spec cannot be created.
    """
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_cli_harness.py"
    spec = importlib.util.spec_from_file_location("run_cli_harness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load CLI harness from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARNESS = load_harness()


class CliHarnessTests(unittest.TestCase):
    """End-to-end subprocess contract checks for the CLI harness."""

    def test_all_harness_cases_pass(self) -> None:
        """Every harness case passes when run in-process."""
        code = HARNESS.run_harness()
        self.assertEqual(code, 0)

    def test_help_subprocess(self) -> None:
        """``--help`` exits 0 and documents ``--from-json``."""
        result = HARNESS.run_cli(["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("--from-json", result.stdout)
        self.assertFalse(result.timed_out)

    def test_transcripts_are_written(self) -> None:
        """Transcript stdout and metadata files are written per case."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "transcripts"
            result = HARNESS.run_cli(["--version"])
            case = HARNESS.Case(name="version", args=["--version"], expect_code=0)
            HARNESS.write_transcript(out, case, result)
            self.assertTrue((out / "version.stdout.txt").is_file())
            self.assertTrue((out / "version.meta.json").is_file())


class LiveScanGuardTests(unittest.TestCase):
    """Ensure the skip-env guard prevents accidental live Registry access."""

    def test_skip_env_blocks_collect_from_registry(self) -> None:
        """``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN`` blocks a live Registry scan."""
        previous = os.environ.get("SOFTWARE_INVENTORY_SKIP_LIVE_SCAN")
        os.environ["SOFTWARE_INVENTORY_SKIP_LIVE_SCAN"] = "1"
        try:
            with self.assertRaises(RuntimeError) as ctx:
                collect_from_registry()
            self.assertIn("SOFTWARE_INVENTORY_SKIP_LIVE_SCAN", str(ctx.exception))
        finally:
            if previous is None:
                os.environ.pop("SOFTWARE_INVENTORY_SKIP_LIVE_SCAN", None)
            else:
                os.environ["SOFTWARE_INVENTORY_SKIP_LIVE_SCAN"] = previous


if __name__ == "__main__":
    unittest.main()
