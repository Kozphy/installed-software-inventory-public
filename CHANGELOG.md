# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CLI process harness (`scripts/run_cli_harness.py`) that drives
  `python -m software_inventory` with fixture snapshots, checks exit codes
  and UTF-8 output, and never touches the live Registry.
- `--from-json PATH` to replay a saved snapshot (legacy array or v1.1
  envelope) without scanning the Registry. Works on any platform.
- `SOFTWARE_INVENTORY_SKIP_LIVE_SCAN` is now enforced in the collector so CI
  and the harness cannot accidentally query Uninstall keys.

## [1.1.0] - 2026-07-17

### Added

- Versioned JSON report envelope (`schema_version` `1.1`) with UTC scan metadata,
  collector source labels, and filter/dedup counts.
- `--legacy-json` flag to emit the previous top-level JSON array shape.
- `diff` subcommand to compare two snapshots (legacy arrays or v1.1 envelopes).
- Diff identity based on normalized name, publisher, and install location
  (version excluded so upgrades appear as changes).
- GitHub Actions CI on `windows-latest` for Python 3.10–3.13.
- PowerShell runner snapshot workflow: timestamped JSON/CSV, `latest.json`,
  previous snapshot retention, and automatic diff reports.
- Unit tests for report envelopes, payload loading, diff behavior, CLI exit
  codes, and non-ASCII names (no live Registry dependency).

### Changed

- Default JSON output is now the v1.1 envelope; table and CSV semantics are
  unchanged.
- Package version bumped to `1.1.0`.
- README documents schema migration, exit codes, privacy of scan metadata, and
  the snapshot/diff workflow.

### Compatibility

- Existing scan CLI flags continue to work without a `scan` subcommand.
- Consumers expecting a JSON array should use `--legacy-json` or read
  `payload["software"]`.
- `diff` accepts both legacy arrays and v1.1 envelopes.

## [1.0.0] - 2026-07-17

### Added

- Initial Windows Uninstall Registry scanner with table/JSON/CSV export,
  filtering, deduplication, PowerShell runner, and unit tests.
