"""Process-level CLI harness for Installed Software Inventory.

Drives ``python -m software_inventory`` as a real subprocess with fixture
JSON instead of the live Windows Registry. Captures exit codes, stdout,
stderr, and optional transcripts. Never writes Registry values.
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
PROCESS_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess result for one harness case."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


@dataclass
class Case:
    """One CLI invocation and the checks that must pass."""

    name: str
    args: Sequence[str]
    expect_code: int
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()
    stdout_not_contains: tuple[str, ...] = ()
    check: Optional[Callable[[CommandResult, Path], Optional[str]]] = None
    extra_env: dict[str, str] = field(default_factory=dict)


def project_python() -> str:
    """Return the interpreter that should run the package under test."""
    return sys.executable


def harness_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Build a deterministic environment that never live-scans the Registry."""
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
    """Run ``python -m software_inventory`` and capture output."""
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
    return json.loads(path.read_text(encoding="utf-8"))


def _check_envelope(result: CommandResult, workdir: Path) -> Optional[str]:
    path = workdir / "scan.json"
    if not path.is_file():
        return f"missing output file {path}"
    payload = _json_payload(path)
    if not isinstance(payload, dict):
        return "expected JSON object envelope"
    if payload.get("schema_version") != "1.1":
        return f"unexpected schema_version {payload.get('schema_version')!r}"
    software = payload.get("software")
    if not isinstance(software, list) or not software:
        return "envelope missing software list"
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


def expected_blocked_live_scan_code() -> int:
    """Live scan without --from-json is unsupported off Windows, skipped on Windows."""
    return 3 if sys.platform != "win32" else 1


def expected_blocked_live_scan_stderr() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ("only runs on Windows",)
    return ("live Registry scan is disabled",)


def build_cases(workdir: Path) -> list[Case]:
    """Return the default CLI contract cases."""
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
            name="blocked-live-scan",
            args=["--format", "json"],
            expect_code=expected_blocked_live_scan_code(),
            stderr_contains=expected_blocked_live_scan_stderr(),
        ),
    ]


def evaluate_case(case: Case, workdir: Path) -> tuple[list[str], CommandResult]:
    """Run one case and return failure messages plus the captured result."""
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
    """Write stdout/stderr/meta for one case when transcripts are requested."""
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
    """Execute all cases. Return 0 when every case passes."""
    if not OLD_JSON.is_file() or not NEW_JSON.is_file() or not LEGACY_JSON.is_file():
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
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return run_harness(transcript_dir=args.keep_transcripts)


if __name__ == "__main__":
    raise SystemExit(main())
