"""
Process-level CLI harness for Installed Software Inventory.

Drives ``python -m software_inventory`` as a real subprocess against fixture
JSON snapshots instead of the live Windows Registry. Validates the public CLI
contract (exit codes, UTF-8 output, formats, ``diff``, live-scan guard) used
by CI and local developers.

Pipeline role (test/QA, not production scan):
    fixture JSON → subprocess CLI → capture exit/stdout/stderr → assert contract

Safety: always sets ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN=1`` and never writes
Registry values.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "cli"
OLD_JSON = FIXTURE_DIR / "old.json"
NEW_JSON = FIXTURE_DIR / "new.json"
LEGACY_JSON = FIXTURE_DIR / "legacy.json"
WINGET_LIST_JSON = FIXTURE_DIR / "winget-list.json"
MIXED_SOURCES_JSON = FIXTURE_DIR / "mixed-sources.json"
PROCESS_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CommandResult:
    """
    Captured subprocess result for one harness case.

    Responsibilities:
        * Hold argv, exit code, streams, timing, and timeout flag for asserts
          and optional transcript files.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


@dataclass
class Case:
    """
    One CLI invocation and the checks that must pass.

    Responsibilities:
        * Describe argv, expected exit code, substring expectations, and an
          optional structured ``check`` callback over the result/workdir.
    """

    name: str
    args: Sequence[str]
    expect_code: int
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()
    stdout_not_contains: tuple[str, ...] = ()
    check: Optional[Callable[[CommandResult, Path], Optional[str]]] = None
    extra_env: dict[str, str] = field(default_factory=dict)


def project_python() -> str:
    """
    Return the interpreter that should run the package under test.

    Returns:
        str: Path to the current ``sys.executable``.
    """
    return sys.executable


def harness_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """
    Build a deterministic environment that never live-scans the Registry.

    Args:
        extra (dict[str, str] | None): Optional overrides merged last.

    Returns:
        dict[str, str]: Env mapping with ``PYTHONPATH``, UTF-8 flags, and
        ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN=1``.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["SOFTWARE_INVENTORY_SKIP_LIVE_SCAN"] = "1"
    if extra:
        env.update(extra)
    return env


def run_cli(
    args: Sequence[str],
    *,
    extra_env: Optional[dict[str, str]] = None,
    timeout: int = PROCESS_TIMEOUT_SECONDS,
) -> CommandResult:
    """
    Run ``python -m software_inventory`` as a subprocess and capture output.

    Args:
        args (Sequence[str]): CLI arguments after the module name.
        extra_env (dict[str, str] | None): Optional env overrides.
        timeout (int): Seconds before the process is treated as hung.

    Returns:
        CommandResult: Captured exit code, streams, and timing. Timeouts use
        returncode ``124`` and ``timed_out=True``.
    """
    argv = (project_python(), "-m", "software_inventory", *args)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            cwd=PROJECT_ROOT,
            env=harness_env(extra_env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if isinstance(exc.stdout, bytes):
            stdout = exc.stdout.decode("utf-8", errors="replace")
        if isinstance(exc.stderr, bytes):
            stderr = exc.stderr.decode("utf-8", errors="replace")
        return CommandResult(
            argv=argv,
            returncode=124,
            stdout=stdout or "",
            stderr=stderr or "error: CLI harness timed out\n",
            duration_ms=duration_ms,
            timed_out=True,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=duration_ms,
    )


def _json_payload(path: Path) -> object:
    """
    Load a UTF-8 JSON file for structured harness assertions.

    Args:
        path (Path): File to parse.

    Returns:
        object: Parsed JSON payload.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _check_envelope(result: CommandResult, workdir: Path) -> Optional[str]:
    """
    Assert the v1.2 JSON envelope written by a harness case is well-formed.

    Args:
        result (CommandResult): Unused process result (signature for Case.check).
        workdir (Path): Temp directory containing ``scan.json``.

    Returns:
        str | None: Failure message, or None when the envelope looks valid.
    """
    path = workdir / "scan.json"
    if not path.is_file():
        return f"missing output file {path}"
    payload = _json_payload(path)
    if not isinstance(payload, dict):
        return "expected JSON object envelope"
    if payload.get("schema_version") != "1.2":
        return f"unexpected schema_version {payload.get('schema_version')!r}"
    software = payload.get("software")
    if not isinstance(software, list) or not software:
        return "envelope missing software list"
    if any(row.get("source") not in {"registry", "appx"} for row in software):
        return "every software row must carry a registry/appx source tag"
    names = {row.get("name") for row in software if isinstance(row, dict)}
    if "日本語エディタ" not in names:
        return "UTF-8 display name missing from JSON envelope"
    scan = payload.get("scan")
    if not isinstance(scan, dict) or "hostname" not in scan:
        return "envelope missing scan metadata"
    if "username" in scan:
        return "scan metadata must not include username"
    return None


def _check_legacy_array(result: CommandResult, workdir: Path) -> Optional[str]:
    """
    Assert ``--legacy-json`` wrote a top-level array with expected content.

    Args:
        result (CommandResult): Unused process result (signature for Case.check).
        workdir (Path): Temp directory containing ``legacy-out.json``.

    Returns:
        str | None: Failure message, or None when the array looks valid.
    """
    path = workdir / "legacy-out.json"
    if not path.is_file():
        return f"missing output file {path}"
    payload = _json_payload(path)
    if not isinstance(payload, list):
        return "expected top-level JSON array for --legacy-json"
    if not payload or payload[0].get("name") != "Legacy App":
        return "legacy array did not round-trip Legacy App"
    return None


def _check_csv(result: CommandResult, workdir: Path) -> Optional[str]:
    """
    Assert CSV export has the expected header and UTF-8 display names.

    Args:
        result (CommandResult): Unused process result (signature for Case.check).
        workdir (Path): Temp directory containing ``scan.csv``.

    Returns:
        str | None: Failure message, or None when CSV looks valid.
    """
    path = workdir / "scan.csv"
    if not path.is_file():
        return f"missing output file {path}"
    text = path.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text else ""
    if not first.startswith("name,"):
        return f"CSV header unexpected: {first!r}"
    if "日本語エディタ" not in text:
        return "UTF-8 display name missing from CSV"
    return None


def _check_diff_json(result: CommandResult, workdir: Path) -> Optional[str]:
    """
    Assert fixture old/new snapshots produce the expected diff summary counts.

    Args:
        result (CommandResult): Unused process result (signature for Case.check).
        workdir (Path): Temp directory containing ``diff.json``.

    Returns:
        str | None: Failure message, or None when summary matches fixtures.
    """
    path = workdir / "diff.json"
    if not path.is_file():
        return f"missing output file {path}"
    payload = _json_payload(path)
    if not isinstance(payload, dict):
        return "expected JSON object for diff"
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return "diff missing summary"
    expected = {"added": 1, "removed": 1, "changed": 1, "unchanged": 2}
    for key, value in expected.items():
        if summary.get(key) != value:
            return f"diff summary {key}={summary.get(key)!r}, expected {value}"
    return None


def _check_winget_bridge(result: CommandResult, workdir: Path) -> Optional[str]:
    """
    Assert ``--format winget`` wrote import JSON and an unmatched checklist.

    Args:
        result (CommandResult): Unused process result (signature for Case.check).
        workdir (Path): Temp directory containing winget outputs.

    Returns:
        str | None: Failure message, or None when outputs look correct.
    """
    path = workdir / "winget-packages.json"
    checklist = workdir / "winget-packages.unmatched.md"
    if not path.is_file():
        return f"missing output file {path}"
    if not checklist.is_file():
        return f"missing unmatched checklist {checklist}"
    payload = _json_payload(path)
    if not isinstance(payload, dict):
        return "expected winget import JSON object"
    if payload.get("$schema") != "https://aka.ms/winget-packages.schema.2.0.json":
        return "winget import missing packages.schema.2.0 $schema"
    sources = payload.get("Sources")
    if not isinstance(sources, list) or not sources:
        return "winget import missing Sources"
    packages = sources[0].get("Packages")
    if not isinstance(packages, list):
        return "winget import missing Packages"
    ids = {row.get("PackageIdentifier") for row in packages if isinstance(row, dict)}
    if "Contoso.KeepApp" not in ids or "Contoso.UpgradeApp" not in ids:
        return f"expected Contoso Keep/Upgrade package ids, got {ids!r}"
    text = checklist.read_text(encoding="utf-8")
    if "New Tool" not in text or "日本語エディタ" not in text:
        return "unmatched checklist missing expected inventory apps"
    if "matched 2" not in result.stderr and "matched 2," not in result.stderr:
        # stderr summary is informational; tolerate wording variants.
        if "matched 2" not in result.stderr:
            return f"stderr missing match summary, got {result.stderr!r}"
    return None


def _check_mixed_merge(result: CommandResult, workdir: Path) -> Optional[str]:
    """
    Assert Registry rows shadow matching Appx rows and frameworks stay hidden.

    Args:
        result (CommandResult): Unused process result (signature for Case.check).
        workdir (Path): Temp directory containing ``mixed.json``.

    Returns:
        str | None: Failure message, or None when the merge policy held.
    """
    path = workdir / "mixed.json"
    if not path.is_file():
        return f"missing output file {path}"
    payload = _json_payload(path)
    if not isinstance(payload, dict):
        return "expected JSON object envelope"
    software = payload.get("software")
    if not isinstance(software, list):
        return "envelope missing software list"
    rows = [(row.get("name"), row.get("source")) for row in software if isinstance(row, dict)]
    expected = [
        ("Dual Packaged App", "registry"),
        ("Keep App", "registry"),
        ("Windows Terminal", "appx"),
    ]
    if rows != expected:
        return f"merged rows {rows!r}, expected {expected!r}"
    scan = payload.get("scan") or {}
    if scan.get("deduplicated_count") != 1:
        return f"deduplicated_count={scan.get('deduplicated_count')!r}, expected 1"
    if scan.get("filtered_system_component_count") != 1:
        return (
            "filtered_system_component_count="
            f"{scan.get('filtered_system_component_count')!r}, expected 1"
        )
    return None


def expected_blocked_live_scan_code() -> int:
    """
    Expected exit code when a live scan is attempted under the skip flag.

    Returns:
        int: ``3`` off Windows (unsupported platform), ``1`` on Windows when
        the collector refuses the live hive.
    """
    return 3 if sys.platform != "win32" else 1


def expected_blocked_live_scan_stderr(collector: str = "Registry") -> tuple[str, ...]:
    """
    Expected stderr needles for a blocked live scan.

    Args:
        collector (str): ``Registry`` or ``Appx`` — which collector refuses.

    Returns:
        tuple[str, ...]: Substrings that must appear on stderr.
    """
    if sys.platform != "win32":
        return ("only runs on Windows",)
    return (f"live {collector} scan is disabled",)


def build_cases(workdir: Path) -> list[Case]:
    """
    Build the default CLI contract cases against fixture snapshots.

    Args:
        workdir (Path): Temp directory for per-case output files.

    Returns:
        list[Case]: Ordered harness cases covering help, formats, diff, and
        live-scan blocking.
    """
    return [
        Case(
            name="help",
            args=["--help"],
            expect_code=0,
            stdout_contains=("Scan local Windows Uninstall Registry", "--from-json"),
        ),
        Case(
            name="version",
            args=["--version"],
            expect_code=0,
            stdout_contains=("software_inventory",),
        ),
        Case(
            name="diff-help",
            args=["diff", "--help"],
            expect_code=0,
            stdout_contains=("Compare two JSON inventory snapshots",),
        ),
        Case(
            name="usage-error",
            args=["--not-a-real-flag"],
            expect_code=2,
            stderr_contains=("unrecognized arguments",),
        ),
        Case(
            name="from-json-table",
            args=["--from-json", str(NEW_JSON), "--format", "table"],
            expect_code=0,
            stdout_contains=("Keep App", "New Tool", "日本語エディタ", "Name"),
            stdout_not_contains=("Gone App",),
        ),
        Case(
            name="from-json-search",
            args=[
                "--from-json",
                str(NEW_JSON),
                "--format",
                "table",
                "--search",
                "keep",
            ],
            expect_code=0,
            stdout_contains=("Keep App",),
            stdout_not_contains=("New Tool",),
        ),
        Case(
            name="from-json-json-envelope",
            args=[
                "--from-json",
                str(NEW_JSON),
                "--format",
                "json",
                "--pretty",
                "--output",
                str(workdir / "scan.json"),
            ],
            expect_code=0,
            check=_check_envelope,
        ),
        Case(
            name="from-json-legacy-json",
            args=[
                "--from-json",
                str(LEGACY_JSON),
                "--format",
                "json",
                "--legacy-json",
                "--pretty",
                "--output",
                str(workdir / "legacy-out.json"),
            ],
            expect_code=0,
            check=_check_legacy_array,
        ),
        Case(
            name="from-json-csv",
            args=[
                "--from-json",
                str(NEW_JSON),
                "--format",
                "csv",
                "--output",
                str(workdir / "scan.csv"),
            ],
            expect_code=0,
            check=_check_csv,
        ),
        Case(
            name="from-json-missing",
            args=["--from-json", str(workdir / "missing.json")],
            expect_code=1,
            stderr_contains=("unable to read",),
        ),
        Case(
            name="diff-table",
            args=["diff", str(OLD_JSON), str(NEW_JSON), "--format", "table"],
            expect_code=0,
            stdout_contains=(
                "Inventory Diff Summary",
                "Added:      1",
                "Removed:    1",
                "Changed:    1",
                "New Tool",
                "Gone App",
                "Upgrade App",
            ),
        ),
        Case(
            name="diff-json",
            args=[
                "diff",
                str(OLD_JSON),
                str(NEW_JSON),
                "--format",
                "json",
                "--pretty",
                "--output",
                str(workdir / "diff.json"),
            ],
            expect_code=0,
            check=_check_diff_json,
        ),
        Case(
            name="diff-missing",
            args=["diff", str(OLD_JSON), str(workdir / "nope.json")],
            expect_code=1,
            stderr_contains=("unable to read",),
        ),
        Case(
            name="from-json-winget",
            args=[
                "--from-json",
                str(NEW_JSON),
                "--format",
                "winget",
                "--winget-list",
                str(WINGET_LIST_JSON),
                "--output",
                str(workdir / "winget-packages.json"),
            ],
            expect_code=0,
            stderr_contains=("matched 2", "unmatched 2"),
            check=_check_winget_bridge,
        ),
        Case(
            name="from-json-source-appx",
            args=[
                "--from-json",
                str(MIXED_SOURCES_JSON),
                "--source",
                "appx",
                "--format",
                "table",
            ],
            expect_code=0,
            stdout_contains=("Windows Terminal", "Source", "appx"),
            stdout_not_contains=("Keep App",),
        ),
        Case(
            name="from-json-mixed-merge",
            args=[
                "--from-json",
                str(MIXED_SOURCES_JSON),
                "--format",
                "json",
                "--output",
                str(workdir / "mixed.json"),
            ],
            expect_code=0,
            check=_check_mixed_merge,
        ),
        Case(
            name="blocked-live-scan",
            args=["--format", "json"],
            expect_code=expected_blocked_live_scan_code(),
            stderr_contains=expected_blocked_live_scan_stderr(),
        ),
        Case(
            name="blocked-live-appx-scan",
            args=["--source", "appx", "--format", "json"],
            expect_code=expected_blocked_live_scan_code(),
            stderr_contains=expected_blocked_live_scan_stderr("Appx"),
        ),
    ]


def evaluate_case(case: Case, workdir: Path) -> tuple[list[str], CommandResult]:
    """
    Run one case and return failure messages plus the captured result.

    Args:
        case (Case): Case definition to execute.
        workdir (Path): Temp directory for output-file checks.

    Returns:
        tuple[list[str], CommandResult]: Failure strings (empty on pass) and
        the subprocess capture.
    """
    result = run_cli(case.args, extra_env=case.extra_env)
    failures: list[str] = []
    if result.timed_out:
        failures.append(f"timed out after {PROCESS_TIMEOUT_SECONDS}s")
    if result.returncode != case.expect_code:
        failures.append(
            f"exit code {result.returncode}, expected {case.expect_code}"
        )
    for needle in case.stdout_contains:
        if needle not in result.stdout:
            failures.append(f"stdout missing {needle!r}")
    for needle in case.stdout_not_contains:
        if needle in result.stdout:
            failures.append(f"stdout unexpectedly contains {needle!r}")
    for needle in case.stderr_contains:
        if needle not in result.stderr:
            failures.append(f"stderr missing {needle!r}")
    if case.check is not None:
        problem = case.check(result, workdir)
        if problem:
            failures.append(problem)
    return failures, result


def write_transcript(directory: Path, case: Case, result: CommandResult) -> None:
    """
    Write stdout/stderr/meta for one case when transcripts are requested.

    Args:
        directory (Path): Destination directory for transcript files.
        case (Case): Case whose ``name`` becomes the file prefix.
        result (CommandResult): Captured subprocess output.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{case.name}.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (directory / f"{case.name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    meta = {
        "name": case.name,
        "argv": list(result.argv),
        "returncode": result.returncode,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }
    (directory / f"{case.name}.meta.json").write_text(
        json.dumps(meta, indent=2) + "\n",
        encoding="utf-8",
    )


def run_harness(transcript_dir: Optional[Path] = None) -> int:
    """
    Execute all harness cases and print a PASS/FAIL summary.

    Args:
        transcript_dir (Path | None): When set, write per-case transcripts here.

    Returns:
        int: ``0`` when every case passes, ``1`` on fixture or assertion failure.
    """
    if (
        not OLD_JSON.is_file()
        or not NEW_JSON.is_file()
        or not LEGACY_JSON.is_file()
        or not WINGET_LIST_JSON.is_file()
        or not MIXED_SOURCES_JSON.is_file()
    ):
        print(
            f"error: harness fixtures missing under {FIXTURE_DIR}",
            file=sys.stderr,
        )
        return 1

    failures_total = 0
    with tempfile.TemporaryDirectory(prefix="software-inventory-harness-") as tmp:
        workdir = Path(tmp)
        cases = build_cases(workdir)
        print(f"CLI harness: {len(cases)} cases (timeout {PROCESS_TIMEOUT_SECONDS}s)")
        for case in cases:
            failures, result = evaluate_case(case, workdir)
            if transcript_dir is not None:
                write_transcript(transcript_dir, case, result)
            status = "PASS" if not failures else "FAIL"
            print(f"  {status}  {case.name}  ({result.duration_ms} ms)")
            for failure in failures:
                print(f"         {failure}")
                failures_total += 1
            if failures:
                if result.stderr.strip():
                    print("         stderr:")
                    for line in result.stderr.strip().splitlines()[:8]:
                        print(f"           {line}")

    if failures_total:
        print(f"CLI harness failed: {failures_total} check(s)")
        return 1
    print("CLI harness passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    Create the argument parser for the harness script itself.

    Returns:
        argparse.ArgumentParser: Parser with optional ``--keep-transcripts``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Drive software_inventory as a subprocess using fixture snapshots. "
            "Never scans the live Windows Registry."
        )
    )
    parser.add_argument(
        "--keep-transcripts",
        type=Path,
        default=None,
        metavar="DIR",
        help="Write per-case stdout/stderr/meta files to DIR.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Parse harness CLI arguments and run the full case suite.

    Args:
        argv (Sequence[str] | None): Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        int: Exit code from ``run_harness``.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return run_harness(transcript_dir=args.keep_transcripts)


if __name__ == "__main__":
    raise SystemExit(main())
