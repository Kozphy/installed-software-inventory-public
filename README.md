# Installed Software Inventory

[![CI](https://github.com/Kozphy/installed-software-inventory/actions/workflows/ci.yml/badge.svg)](https://github.com/Kozphy/installed-software-inventory/actions/workflows/ci.yml)

Safe, local-first command-line tool that scans a Windows computer for installed software and exports the results to **table**, **JSON**, or **CSV** formats. Version **1.1** adds a versioned JSON report envelope, snapshot comparison (`diff`), and Windows CI.

## Purpose

This project reads traditional Windows **Uninstall** Registry keys to build an inventory of installed applications. It is designed for personal audits, asset tracking, and documentation—without network access, without administrator rights for normal use, and without modifying the system.

## Safety and privacy

- **Read-only**: the tool never writes Registry values and never runs uninstall commands.
- **Local-first**: all data stays on your machine; the tool makes **no network requests**.
- **No Win32_Product**: the WMI `Win32_Product` class is intentionally avoided because querying it can trigger Windows Installer repair or consistency checks.
- **No elevation required** for normal scans of readable Uninstall keys (some protected keys may simply be skipped).
- **Scan metadata** in JSON reports includes hostname, platform string, timestamps, and filter counts. It does **not** include Windows usernames, email addresses, or other account identifiers. Hostname is included so fleet snapshots can be distinguished; omit or redact it downstream if that is too identifying for your use case.

## Requirements

| Requirement | Details |
|-------------|---------|
| Operating system | Windows 10/11 (or Windows Server with standard Uninstall keys) |
| Python | 3.10 or newer |
| Privileges | Standard user is sufficient for normal use |
| Extra packages | None required for runtime (stdlib only) |

## Installation

```powershell
cd installed-software-inventory
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

If you prefer not to install, set `PYTHONPATH` to the `src` folder:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m software_inventory --help
```

## CLI examples

```powershell
python -m software_inventory
python -m software_inventory --format table
python -m software_inventory --format json
python -m software_inventory --format json --pretty --output reports/software.json
python -m software_inventory --format json --legacy-json --output reports/legacy.json
python -m software_inventory --format csv --output reports/software.csv
python -m software_inventory --search microsoft
python -m software_inventory --include-system-components
python -m software_inventory --include-updates
python -m software_inventory --format json --pretty --verbose
```

### Snapshot comparison

```powershell
python -m software_inventory diff reports/old.json reports/new.json
python -m software_inventory diff reports/old.json reports/new.json --format table
python -m software_inventory diff reports/old.json reports/new.json --format json
python -m software_inventory diff reports/old.json reports/new.json --format json --pretty --output reports/diff.json
```

### Scan arguments

| Argument | Description |
|----------|-------------|
| `--format table\|json\|csv` | Output format (default: `table`) |
| `--output PATH` | Write to a file instead of stdout (creates parent dirs) |
| `--search TEXT` | Case-insensitive match on name, publisher, or version |
| `--include-system-components` | Show entries marked `SystemComponent` |
| `--include-updates` | Show Windows updates / hotfixes |
| `--pretty` | Indent JSON output |
| `--legacy-json` | Emit a top-level JSON array (v1.0 shape) instead of the v1.1 envelope |
| `--verbose` | Detailed diagnostics on stderr |
| `--version` | Print package version |

### Default filters

1. Entries without a valid display name are dropped.
2. System components are hidden unless `--include-system-components` is set.
3. Updates and hotfixes are hidden unless `--include-updates` is set.
4. Duplicate records from overlapping Registry views are merged (richest metadata wins).
5. Results are sorted alphabetically by application name.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Runtime / I/O / invalid snapshot input |
| `2` | Usage error (argparse) |
| `3` | Unsupported platform (non-Windows for scans) |

`diff` works on any platform that can read the JSON files; live Registry scanning requires Windows.

## JSON schema (v1.1)

Default JSON output is a **versioned report envelope**:

```json
{
  "schema_version": "1.1",
  "scan": {
    "started_at": "2026-07-17T06:00:00Z",
    "completed_at": "2026-07-17T06:00:01Z",
    "duration_ms": 1000,
    "hostname": "WORKSTATION-01",
    "platform": "Windows-10-10.0.26200-SP0",
    "collector_sources": [
      "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall [64-bit view]"
    ],
    "raw_entry_count": 100,
    "deduplicated_count": 5,
    "filtered_system_component_count": 10,
    "filtered_update_count": 8,
    "result_count": 77
  },
  "software": [
    {
      "name": "Example Editor",
      "version": "1.2.3",
      "publisher": "Example Inc",
      "install_date": "2026-07-17",
      "install_location": "C:\\Program Files\\Example Editor",
      "estimated_size_kb": 870400,
      "scope": "machine",
      "architecture": "64-bit",
      "uninstall_string": "C:\\Program Files\\Example Editor\\uninstall.exe",
      "quiet_uninstall_string": "C:\\Program Files\\Example Editor\\uninstall.exe /S",
      "registry_path": "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\ExampleEditor",
      "release_type": null,
      "system_component": false
    }
  ]
}
```

Timestamps are UTC ISO-8601 with a `Z` suffix. Software entries inside `software` keep the same field semantics as v1.0. Table and CSV exports are unchanged.

### Migration from v1.0 JSON arrays

Consumers that expect a **top-level array** should either:

1. Pass `--legacy-json` when exporting, or
2. Read `payload["software"]` when `schema_version` is present.

Both shapes are accepted as input to `diff`.

## Snapshot workflow

Recommended automation loop:

1. Export a JSON snapshot (envelope).
2. Keep `reports/latest.json` as the last known good scan.
3. On the next run, compare previous vs new with `diff`.
4. Archive timestamped JSON/CSV/diff files.

`scripts/run_inventory.ps1` implements this workflow:

```powershell
.\scripts\run_inventory.ps1
```

It writes:

```text
reports/installed-software-YYYY-MM-DD-HHMMSS.json
reports/installed-software-YYYY-MM-DD-HHMMSS.csv
reports/latest.json
reports/previous.json                 # copy of prior latest, when present
reports/installed-software-diff-YYYY-MM-DD-HHMMSS.json
```

The script does **not** change the PowerShell execution policy. If scripts are blocked:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_inventory.ps1
```

## Example diff output

```text
Inventory Diff Summary
======================
Added:      1
Removed:    1
Changed:    1
Unchanged:  40

Added
-----
  + New Tool (1.0.0) — Vendor Inc

Removed
-------
  - Old Tool (2.1) — Vendor Inc

Changed
-------
  ~ Example Editor: 1.2.3 → 1.3.0
      version: '1.2.3' → '1.3.0'
```

Diff identity uses normalized **name + publisher + install location** (version excluded so upgrades appear as changes).

## Collected fields

| Field | Description |
|-------|-------------|
| `name` | Application display name (`DisplayName`) |
| `version` | Display version string |
| `publisher` | Publisher / vendor |
| `install_date` | Normalized `YYYY-MM-DD`, or `null` if unknown/invalid |
| `install_location` | Install folder when present |
| `estimated_size_kb` | Registry `EstimatedSize` in KB (original units preserved) |
| `scope` | `machine` (HKLM) or `current_user` (HKCU) |
| `architecture` | `64-bit`, `32-bit`, or `unknown` based on Registry view |
| `uninstall_string` | Uninstall command (reported only; never executed) |
| `quiet_uninstall_string` | Quiet uninstall command when present |
| `registry_path` | Full path of the Uninstall subkey used as the source |
| `release_type` | Registry `ReleaseType` when present |
| `system_component` | Whether `SystemComponent` is set |

## Registry sources

- `HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`
- `HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall`
- `HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`

Both 32-bit and 64-bit Registry views are used where applicable.

## Registry limitations

Traditional Uninstall keys do **not** capture every program on a PC:

- Some **Microsoft Store** (UWP / MSIX) applications
- **Portable** programs that never register an Uninstall key
- Software installed only through **custom package managers**
- Entries with empty `DisplayName` (filtered out by design)
- Values the current user cannot read (skipped quietly)

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `only runs on Windows` | Live scans require Windows; `diff` works on saved JSON anywhere. |
| `Python was not found` | Install Python 3.10+ and ensure `python` is on `PATH`. |
| `unsupported schema_version` | Use a v1.1 envelope or a legacy array; upgrade the tool if needed. |
| Empty or sparse results | Try `--include-system-components` / `--include-updates`. |
| Garbled console text | Prefer `--output` files (UTF-8); some consoles use legacy code pages. |
| Module not found | Install with `pip install -e .` or set `PYTHONPATH` to `src`. |

## Example CSV output

```csv
name,version,publisher,install_date,install_location,estimated_size_kb,scope,architecture,uninstall_string,quiet_uninstall_string,registry_path,release_type,system_component
Example Editor,1.2.3,Example Inc,2026-07-17,C:\Program Files\Example Editor,870400,machine,64-bit,C:\Program Files\Example Editor\uninstall.exe,C:\Program Files\Example Editor\uninstall.exe /S,HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ExampleEditor,,False
```

## Project layout

```text
installed-software-inventory/
├── README.md
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
├── .gitignore
├── .github/workflows/ci.yml
├── scripts/
│   └── run_inventory.ps1
├── src/
│   └── software_inventory/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── models.py
│       ├── normalize.py
│       ├── exporters.py
│       ├── report.py
│       ├── diff.py
│       └── collectors/
│           ├── __init__.py
│           └── windows_registry.py
└── tests/
    ├── test_normalize.py
    ├── test_deduplication.py
    ├── test_exporters.py
    ├── test_registry_parsing.py
    ├── test_report.py
    └── test_diff.py
```

## License

MIT — see [LICENSE](LICENSE).
