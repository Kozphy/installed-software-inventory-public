<#
.SYNOPSIS
    Run Installed Software Inventory and write timestamped JSON/CSV reports.

.DESCRIPTION
    Creates a reports directory (if needed), invokes the Python inventory tool,
    and writes paired JSON and CSV exports. Does not change the PowerShell
    execution policy.

.EXAMPLE
    .\scripts\run_inventory.ps1
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$ReportsDir = Join-Path $ProjectRoot "reports"
$SrcDir = Join-Path $ProjectRoot "src"

function Test-PythonAvailable {
    try {
        $null = & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

if (-not (Test-PythonAvailable)) {
    Write-Host @"
Python 3.10+ was not found on PATH.

Install Python from https://www.python.org/downloads/ and ensure
'python' is available in this terminal, then run this script again.
"@ -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $ReportsDir)) {
    New-Item -ItemType Directory -Path $ReportsDir | Out-Null
}

$Stamp = Get-Date -Format "yyyy-MM-dd-HHmmss"
$JsonPath = Join-Path $ReportsDir "installed-software-$Stamp.json"
$CsvPath = Join-Path $ReportsDir "installed-software-$Stamp.csv"

$env:PYTHONPATH = $SrcDir

Write-Host "Scanning installed software..."
& python -m software_inventory --format json --pretty --output $JsonPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "JSON export failed with exit code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}

& python -m software_inventory --format csv --output $CsvPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "CSV export failed with exit code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Reports written:"
Write-Host "  JSON: $JsonPath"
Write-Host "  CSV:  $CsvPath"
