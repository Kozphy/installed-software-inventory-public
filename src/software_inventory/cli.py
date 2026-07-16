"""Command-line interface for Installed Software Inventory."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from software_inventory import __version__
from software_inventory.collectors import collect_from_registry
from software_inventory.exporters import export_csv, export_json, export_table
from software_inventory.normalize import prepare_inventory

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME = 1
EXIT_UNSUPPORTED = 3


def configure_stdio() -> None:
    """Prefer UTF-8 on stdout/stderr so non-ASCII display names print safely."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser for the inventory CLI."""
    parser = argparse.ArgumentParser(
        prog="software_inventory",
        description=(
            "Scan local Windows Uninstall Registry keys and list installed "
            "software. Read-only: never modifies the Registry or uninstalls apps."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="Output format (default: table).",
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


def configure_logging(verbose: bool) -> None:
    """Configure root logging; detailed diagnostics only when verbose."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def run_inventory(
    *,
    format_name: str = "table",
    output: Optional[Path] = None,
    search: Optional[str] = None,
    include_system_components: bool = False,
    include_updates: bool = False,
    pretty: bool = False,
    entries=None,
) -> int:
    """Collect (or accept), prepare, and export inventory entries.

    Returns a process exit code.
    """
    try:
        raw = list(entries) if entries is not None else collect_from_registry()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except Exception as exc:  # pragma: no cover - unexpected failures
        logging.getLogger(__name__).exception("Inventory collection failed")
        print(f"error: failed to collect installed software: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    prepared = prepare_inventory(
        raw,
        include_system_components=include_system_components,
        include_updates=include_updates,
        search=search,
    )

    try:
        if format_name == "table":
            export_table(prepared, output=output)
        elif format_name == "json":
            export_json(prepared, output=output, pretty=pretty)
        elif format_name == "csv":
            export_csv(prepared, output=output)
        else:  # pragma: no cover - argparse restricts choices
            print(f"error: unsupported format {format_name!r}", file=sys.stderr)
            return EXIT_USAGE
    except OSError as exc:
        print(f"error: failed to write output: {exc}", file=sys.stderr)
        return EXIT_RUNTIME

    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse CLI arguments and run the inventory scan."""
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    configure_logging(args.verbose)

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
    )


if __name__ == "__main__":
    raise SystemExit(main())
