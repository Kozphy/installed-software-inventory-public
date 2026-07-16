# Installed Software Inventory

Safe, local-first command-line tool that scans a Windows computer for installed software and exports the results to **table**, **JSON**, or **CSV** formats.

## Purpose

This project reads traditional Windows **Uninstall** Registry keys to build an inventory of installed applications. It is designed for personal audits, asset tracking, and documentation—without network access, without administrator rights for normal use, and without modifying the system.

## Safety and privacy

- **Read-only**: the tool never writes Registry values and never runs uninstall commands.
- **Local-first**: all data stays on your machine; the tool makes **no network requests**.
- **No Win32_Product**: the WMI `Win32_Product` class is intentionally avoided because querying it can trigger Windows Installer repair or consistency checks.
- **No elevation required** for normal scans of readable Uninstall keys (some protected keys may simply be skipped).

## Requirements

| Requirement | Details |
|-------------|---------|
| Operating system | Windows 10/11 (or Windows Server with standard Uninstall keys) |
| Python | 3.10 or newer |
| Privileges | Standard user is sufficient for normal use |
| Extra packages | None required for runtime (stdlib only) |

## Installation

Clone or copy this repository, then install the package in editable mode (optional but recommended):

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

## Virtual-environment setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run tests:

```powershell
python -m pytest
# or
python -m unittest discover -s tests -v
```

## CLI examples

```powershell
python -m software_inventory
python -m software_inventory --format table
python -m software_inventory --format json
python -m software_inventory --format csv
python -m software_inventory --format json --output reports/software.json
python -m software_inventory --format csv --output reports/software.csv
python -m software_inventory --search microsoft
python -m software_inventory --include-system-components
python -m software_inventory --include-updates
python -m software_inventory --format json --pretty --verbose
```

### Arguments

| Argument | Description |
|----------|-------------|
| `--format table\|json\|csv` | Output format (default: `table`) |
| `--output PATH` | Write to a file instead of stdout (creates parent dirs) |
| `--search TEXT` | Case-insensitive match on name, publisher, or version |
| `--include-system-components` | Show entries marked `SystemComponent` |
| `--include-updates` | Show Windows updates / hotfixes |
| `--pretty` | Indent JSON output |
| `--verbose` | Detailed diagnostics on stderr |
| `--version` | Print package version |

### Default filters

1. Entries without a valid display name are dropped.
2. System components are hidden unless `--include-system-components` is set.
3. Updates and hotfixes are hidden unless `--include-updates` is set.
4. Duplicate records from overlapping Registry views are merged (richest metadata wins).
5. Results are sorted alphabetically by application name.

Exit codes: `0` success, `1` runtime/IO failure, `2` usage error (argparse), `3` unsupported platform.

## PowerShell runner

`scripts/run_inventory.ps1` creates a `reports` folder and writes timestamped JSON and CSV files:

```powershell
.\scripts\run_inventory.ps1
```

Example filenames:

```text
installed-software-2026-07-17-143000.json
installed-software-2026-07-17-143000.csv
```

The script does **not** change the PowerShell execution policy. If scripts are blocked, run with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_inventory.ps1
```

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

Table output shows a human-readable size (for example `850 MB`, `1.4 GB`) derived from `estimated_size_kb`.

## Registry sources

The scanner reads:

- `HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`
- `HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall`
- `HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`

Both 32-bit and 64-bit Registry views are used where applicable.

## Registry limitations

Traditional Uninstall keys do **not** capture every program on a PC. The following may be missing or incomplete:

- Some **Microsoft Store** (UWP / MSIX) applications
- **Portable** programs that never register an Uninstall key
- Software installed only through **custom package managers** or side-by-side toolchains
- Entries with empty `DisplayName` (filtered out by design)
- Values that the current user cannot read (skipped quietly)

Install dates and sizes are only as accurate as the Registry data written by each installer. Missing values never crash the scan; they appear as `null` / empty fields.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `only runs on Windows` | Use a Windows host; non-Windows platforms are unsupported. |
| `Python was not found` | Install Python 3.10+ and ensure `python` is on `PATH`. |
| Empty or sparse results | Try `--include-system-components` / `--include-updates`, or confirm apps register Uninstall keys. |
| Permission / access errors | The tool skips unreadable keys; re-run with `--verbose` to see skipped paths. |
| Garbled CSV in Excel | File is UTF-8; use Excel’s import wizard or open via Data → From Text/CSV. |
| Module not found | Install with `pip install -e .` or set `PYTHONPATH` to `src`. |

## Example JSON output

```json
[
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
```

## Example CSV output

```csv
name,version,publisher,install_date,install_location,estimated_size_kb,scope,architecture,uninstall_string,quiet_uninstall_string,registry_path,release_type,system_component
Example Editor,1.2.3,Example Inc,2026-07-17,C:\Program Files\Example Editor,870400,machine,64-bit,C:\Program Files\Example Editor\uninstall.exe,C:\Program Files\Example Editor\uninstall.exe /S,HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ExampleEditor,,False
```

## Project layout

```text
installed-software-inventory/
├── README.md
├── LICENSE
├── pyproject.toml
├── .gitignore
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
│       └── collectors/
│           ├── __init__.py
│           └── windows_registry.py
└── tests/
    ├── test_normalize.py
    ├── test_deduplication.py
    └── test_exporters.py
```

## License

MIT — see [LICENSE](LICENSE).
