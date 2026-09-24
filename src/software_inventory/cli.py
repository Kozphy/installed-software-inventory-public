"""
Command-line interface — the product surface for humans and automation.

Wires the inventory pipeline and the snapshot ``diff`` subcommand:

    Scan:
        flags → Registry + Appx (``--source``) | ``--from-json``
            → prepare → report → table/json/csv/winget
    Diff:
        OLD.json + NEW.json → compare → table/json

Public exit-code contract (do not change lightly):
    0 success · 1 runtime/I/O (incl. skip-live-scan RuntimeError on Windows) ·
    2 usage (argparse) · 3 unsupported platform for live scans.

Read-only by design: never modifies the Registry, packages, or uninstalls
software. ``--format winget`` only runs ``winget list`` (read) and never
imports; the Appx collector only enumerates packages.
"""


from __future__ import annotations

import argparse
import logging
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from software_inventory import __version__
from software_inventory.collectors import (
    SOURCE_CHOICES,
    collect_inventory,
    describe_collector_sources,
)
from software_inventory.diff import compare_inventories, load_inventory_file
from software_inventory.exporters import (
    export_csv,
    export_diff,
    export_json,
    export_table,
    export_winget,
)
from software_inventory.normalize import prepare_inventory_with_stats
from software_inventory.report import build_report, build_scan_metadata
from software_inventory.winget_bridge import (
    default_unmatched_path,
    load_winget_packages_json,
    match_inventory_to_winget,
    run_winget_list,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME = 1
EXIT_UNSUPPORTED = 3


def configure_stdio() -> None:
    """
    Prefer UTF-8 on stdout/stderr so non-ASCII display names print safely.

    Best-effort only: failures are ignored because some hosts lack
    ``reconfigure`` or reject encoding changes mid-stream.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def configure_logging(verbose: bool) -> None:
    """
    Configure root logging; detailed diagnostics only when verbose.

    Args:
        verbose (bool): When True, set DEBUG; otherwise WARNING on stderr.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def build_scan_parser() -> argparse.ArgumentParser:
    """
    Create the argument parser for inventory scans.

    Returns:
        argparse.ArgumentParser: Parser for format/output/filter/from-json flags.
    """
    parser = argparse.ArgumentParser(
        prog="software_inventory",
        description=(
            "Scan local Windows Uninstall Registry keys and Appx/MSIX (Microsoft "
            "Store) packages and list installed software. Read-only: never "
            "modifies the Registry or packages, or uninstalls apps. "
            "Use 'software_inventory diff OLD.json NEW.json' to compare snapshots."
        ),
    )
    parser.add_argument(
        "--source",
        choices=SOURCE_CHOICES,
        default="all",
        help=(
            "Collectors to run (default: all). 'registry' = Uninstall keys, "
            "'appx' = Store/MSIX packages. With --from-json, keeps only rows "
            "with that source tag."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("table", "json", "csv", "winget"),
        default="table",
        help=(
            "Output format (default: table). "
            "'winget' emits a packages.schema.2.0 import JSON plus unmatched checklist."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write results to PATH instead of stdout.",
    )
    parser.add_argument(
        "--search",
        default=None,
        metavar="TEXT",
        help="Case-insensitive filter across name, publisher, and version.",
    )
    parser.add_argument(
        "--include-system-components",
        action="store_true",
        help="Include entries marked as system components.",
    )
    parser.add_argument(
        "--include-updates",
        action="store_true",
        help="Include Windows updates and hotfixes.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (indent=2).",
    )
    parser.add_argument(
        "--legacy-json",
        action="store_true",
        help="Emit a top-level JSON array instead of the v1.1 report envelope.",
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Load a JSON snapshot instead of scanning the Registry "
            "(legacy array or v1.1 envelope). Works on any platform."
        ),
    )
    parser.add_argument(
        "--winget-list",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "JSON fixture of winget packages (name/id/version/source) used instead "
            "of running 'winget list'. Required for offline --format winget in CI."
        ),
    )
    parser.add_argument(
        "--include-versions",
        action="store_true",
        help="When --format winget, pin Version on each PackageIdentifier.",
    )
    parser.add_argument(
        "--unmatched-output",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Markdown checklist of inventory apps with no winget Id "
            "(default: <output-stem>.unmatched.md when --output is set)."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed diagnostic logging on stderr.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def build_diff_parser() -> argparse.ArgumentParser:
    """
    Create the argument parser for snapshot comparison.

    Returns:
        argparse.ArgumentParser: Parser for ``diff OLD.json NEW.json`` options.
    """
    parser = argparse.ArgumentParser(
        prog="software_inventory diff",
        description="Compare two JSON inventory snapshots and report changes.",
    )
    parser.add_argument(
        "old_path",
        type=Path,
        metavar="OLD.json",
        help="Previous inventory snapshot (legacy array or v1.1 envelope).",
    )
    parser.add_argument(
        "new_path",
        type=Path,
        metavar="NEW.json",
        help="Current inventory snapshot (legacy array or v1.1 envelope).",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Diff output format (default: table).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write the diff to PATH instead of stdout.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON diff output (indent=2).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed diagnostic logging on stderr.",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    """
    Return the default scan parser (used by tests and ``--help``).

    Returns:
        argparse.ArgumentParser: Same object as ``build_scan_parser()``.
    """
    return build_scan_parser()


def resolve_hostname() -> str:
    """
    Resolve the local hostname without raising on resolution failure.

    Returns:
        str: Hostname string, or ``unknown`` when lookup fails or is empty.
    """
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def run_inventory(
    *,
    format_name: str = "table",
    output: Optional[Path] = None,
    search: Optional[str] = None,
    include_system_components: bool = False,
    include_updates: bool = False,
    pretty: bool = False,
    legacy_json: bool = False,
    include_versions: bool = False,
    winget_list: Optional[Path] = None,
    unmatched_output: Optional[Path] = None,
    source: str = "all",
    entries=None,
    hostname: Optional[str] = None,
    platform_name: Optional[str] = None,
    collector_sources: Optional[Sequence[str]] = None,
) -> int:
    """
    Run one inventory pass: collect/accept → prepare → export.

    Shared by live Registry scans, ``--from-json`` replay, and unit tests that
    inject ``entries``. Timing in scan metadata spans collection through prepare
    (export I/O is outside ``duration_ms``).

    Args:
        format_name (str): ``table``, ``json``, ``csv``, or ``winget``.
        output (Path | None): Optional output file path.
        search (str | None): Optional case-insensitive search needle.
        include_system_components (bool): Keep SystemComponent rows when True.
        include_updates (bool): Keep update/hotfix rows when True.
        pretty (bool): Pretty-print JSON when True.
        legacy_json (bool): Emit top-level JSON array when True.
        include_versions (bool): Pin versions in winget import JSON when True.
        winget_list (Path | None): Offline winget package fixture for
            ``--format winget``; when None, runs live ``winget list``.
        unmatched_output (Path | None): Checklist path for unmatched apps.
        source (str): ``all``, ``registry``, or ``appx`` collectors for a
            live scan; ignored when ``entries`` is given.
        entries: Preloaded ``SoftwareEntry`` iterable; when None, calls
            ``collect_inventory(source)``.
        hostname (str | None): Override for scan metadata hostname.
        platform_name (str | None): Override for scan metadata platform.
        collector_sources (Sequence[str] | None): Override source labels
            (``--from-json`` stamps ``json-snapshot:…`` here).

    Returns:
        int: ``EXIT_OK`` on success. ``OSError`` from collection →
        ``EXIT_UNSUPPORTED``; other collection failures (including live-scan
        skip ``RuntimeError``) → ``EXIT_RUNTIME``; write failures →
        ``EXIT_RUNTIME``; unknown format → ``EXIT_USAGE``.
    """
    started_at = datetime.now(timezone.utc)
    collected_sources: Optional[list[str]] = None
    try:
        if entries is not None:
            raw = list(entries)
        else:
            raw, collected_sources = collect_inventory(source)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except Exception as exc:  # pragma: no cover - unexpected failures
        logging.getLogger(__name__).exception("Inventory collection failed")
        print(f"error: failed to collect installed software: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    prepared, stats = prepare_inventory_with_stats(
        raw,
        include_system_components=include_system_components,
        include_updates=include_updates,
        search=search,
    )
    completed_at = datetime.now(timezone.utc)

    if collector_sources is not None:
        sources = list(collector_sources)
    elif collected_sources is not None:
        sources = collected_sources
    else:
        sources = describe_collector_sources()
    scan = build_scan_metadata(
        started_at=started_at,
        completed_at=completed_at,
        hostname=hostname if hostname is not None else resolve_hostname(),
        platform=platform_name if platform_name is not None else platform.platform(),
        collector_sources=sources,
        stats=stats,
    )
    report = build_report(prepared, scan)

    try:
        if format_name == "table":
            export_table(prepared, output=output)
        elif format_name == "json":
            export_json(
                prepared,
                output=output,
                pretty=pretty,
                report=report,
                legacy_json=legacy_json,
            )
        elif format_name == "csv":
            export_csv(prepared, output=output)
        elif format_name == "winget":
            return _export_winget_bridge(
                prepared,
                output=output,
                pretty=True,
                include_versions=include_versions,
                winget_list=winget_list,
                unmatched_output=unmatched_output,
            )
        else:  # pragma: no cover - argparse restricts choices
            print(f"error: unsupported format {format_name!r}", file=sys.stderr)
            return EXIT_USAGE
    except OSError as exc:
        print(f"error: failed to write output: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    return EXIT_OK


def _export_winget_bridge(
    prepared,
    *,
    output: Optional[Path],
    pretty: bool,
    include_versions: bool,
    winget_list: Optional[Path],
    unmatched_output: Optional[Path],
) -> int:
    """
    Load winget packages, match inventory, and write import JSON + checklist.

    Args:
        prepared: Prepared ``SoftwareEntry`` rows to match.
        output (Path | None): Import JSON path; stdout when None.
        pretty (bool): Indent the import JSON.
        include_versions (bool): Pin versions in the import document.
        winget_list (Path | None): Offline package fixture; runs live
            ``winget list`` when None.
        unmatched_output (Path | None): Checklist path; defaults to a
            ``*.unmatched.md`` sidecar next to ``output``.

    Returns:
        int: ``EXIT_OK`` or ``EXIT_RUNTIME`` on winget/I/O failures.
    """
    try:
        if winget_list is not None:
            packages = load_winget_packages_json(winget_list)
        else:
            packages = run_winget_list()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as exc:  # pragma: no cover - unexpected winget failures
        logging.getLogger(__name__).exception("winget list failed")
        print(f"error: winget list failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    result = match_inventory_to_winget(prepared, packages)
    checklist_path = unmatched_output
    if checklist_path is None and output is not None:
        checklist_path = default_unmatched_path(output)

    try:
        export_winget(
            result,
            output=output,
            pretty=pretty,
            include_versions=include_versions,
            unmatched_output=checklist_path,
        )
    except OSError as exc:
        print(f"error: failed to write output: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    print(
        f"winget bridge: matched {result.matched_count}, "
        f"unmatched {result.unmatched_count}"
        + (f", checklist {checklist_path}" if checklist_path is not None else ""),
        file=sys.stderr,
    )
    return EXIT_OK


def run_diff(
    *,
    old_path: Path,
    new_path: Path,
    format_name: str = "table",
    output: Optional[Path] = None,
    pretty: bool = False,
) -> int:
    """
    Compare two inventory snapshots and export the diff.

    Args:
        old_path (Path): Previous snapshot JSON path.
        new_path (Path): Current snapshot JSON path.
        format_name (str): ``table`` or ``json``.
        output (Path | None): Optional output file path.
        pretty (bool): Pretty-print JSON when True.

    Returns:
        int: ``EXIT_OK`` on success, ``EXIT_RUNTIME`` on load/write failures,
        ``EXIT_USAGE`` on unsupported format.
    """
    try:
        old_entries = load_inventory_file(old_path)
        new_entries = load_inventory_file(new_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    result = compare_inventories(old_entries, new_entries)
    try:
        export_diff(
            result,
            format_name=format_name,
            output=output,
            pretty=pretty,
        )
    except OSError as exc:
        print(f"error: failed to write output: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Dispatch ``diff`` vs scan, including ``--from-json`` replay on any OS.

    Args:
        argv (Sequence[str] | None): Args without the program name; defaults to
            ``sys.argv[1:]``.

    Returns:
        int: Exit code from ``run_diff`` / ``run_inventory``, or
        ``EXIT_UNSUPPORTED`` when a live scan is requested off Windows.

    Notes:
        Platform gate applies only to live Registry scans. ``diff`` and
        ``--from-json`` are intentionally cross-platform so CI can exercise the
        CLI without a Windows hive.
    """
    configure_stdio()
    args_list = list(argv) if argv is not None else sys.argv[1:]

    if args_list and args_list[0] == "diff":
        parser = build_diff_parser()
        args = parser.parse_args(args_list[1:])
        configure_logging(args.verbose)
        return run_diff(
            old_path=args.old_path,
            new_path=args.new_path,
            format_name=args.format,
            output=args.output,
            pretty=args.pretty,
        )

    parser = build_scan_parser()
    args = parser.parse_args(args_list)
    configure_logging(args.verbose)

    if args.from_json is not None:
        try:
            entries = load_inventory_file(args.from_json)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_RUNTIME
        if args.source != "all":
            entries = [entry for entry in entries if entry.source == args.source]
        return run_inventory(
            format_name=args.format,
            output=args.output,
            search=args.search,
            include_system_components=args.include_system_components,
            include_updates=args.include_updates,
            pretty=args.pretty,
            legacy_json=args.legacy_json,
            include_versions=args.include_versions,
            winget_list=args.winget_list,
            unmatched_output=args.unmatched_output,
            entries=entries,
            collector_sources=[f"json-snapshot:{args.from_json}"],
        )

    if sys.platform != "win32":
        print(
            "error: Installed Software Inventory only runs on Windows. "
            f"Detected platform: {sys.platform!r}.",
            file=sys.stderr,
        )
        return EXIT_UNSUPPORTED

    return run_inventory(
        format_name=args.format,
        output=args.output,
        search=args.search,
        include_system_components=args.include_system_components,
        include_updates=args.include_updates,
        pretty=args.pretty,
        legacy_json=args.legacy_json,
        include_versions=args.include_versions,
        winget_list=args.winget_list,
        unmatched_output=args.unmatched_output,
        source=args.source,
    )


if __name__ == "__main__":
    raise SystemExit(main())
