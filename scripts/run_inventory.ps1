<#
.SYNOPSIS
    Run Installed Software Inventory and write timestamped JSON/CSV reports.

.DESCRIPTION
    Creates a reports directory (if needed), writes timestamped JSON and CSV
    snapshots, maintains reports/latest.json, and compares against the previous
    latest snapshot when available. Does not change the PowerShell execution
    policy.

.EXAMPLE
    .\scripts\run_inventory.ps1
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$ReportsDir = Join-Path -Path $ProjectRoot -ChildPath "reports"
$SrcDir = Join-Path -Path $ProjectRoot -ChildPath "src"
$LatestPath = Join-Path -Path $ReportsDir -ChildPath "latest.json"
$PreviousPath = Join-Path -Path $ReportsDir -ChildPath "previous.json"

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
$JsonPath = Join-Path -Path $ReportsDir -ChildPath "installed-software-$Stamp.json"
$CsvPath = Join-Path -Path $ReportsDir -ChildPath "installed-software-$Stamp.csv"
$DiffPath = Join-Path -Path $ReportsDir -ChildPath "installed-software-diff-$Stamp.json"

$env:PYTHONPATH = $SrcDir

$HadPrevious = $false
if (Test-Path -LiteralPath $LatestPath) {
    Copy-Item -LiteralPath $LatestPath -Destination $PreviousPath -Force
    $HadPrevious = $true
}

Write-Host "Scanning installed software..."
& python -m software_inventory --format json --pretty --output "$JsonPath"
if ($LASTEXITCODE -ne 0) {
    Write-Host "JSON export failed with exit code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}

& python -m software_inventory --format csv --output "$CsvPath"
if ($LASTEXITCODE -ne 0) {
    Write-Host "CSV export failed with exit code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}

Copy-Item -LiteralPath $JsonPath -Destination $LatestPath -Force

$WingetPath = Join-Path -Path $ReportsDir -ChildPath "winget-packages-$Stamp.json"
$ChecklistPath = Join-Path -Path $ReportsDir -ChildPath "winget-packages-$Stamp.unmatched.md"
Write-Host "Building winget import + unmatched checklist..."
& python -m software_inventory --from-json "$JsonPath" --format winget --output "$WingetPath"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Winget bridge failed with exit code $LASTEXITCODE (is winget installed?)." -ForegroundColor Yellow
}
else {
    Write-Host "  Winget:    $WingetPath"
    if (Test-Path -LiteralPath $ChecklistPath) {
        Write-Host "  Unmatched: $ChecklistPath"
    }
}

Write-Host ""
Write-Host "Reports written:"
Write-Host "  JSON:   $JsonPath"
Write-Host "  CSV:    $CsvPath"
Write-Host "  Latest: $LatestPath"

if ($HadPrevious) {
    Write-Host ""
    Write-Host "Comparing previous snapshot with new scan..."
    & python -m software_inventory diff "$PreviousPath" "$JsonPath" --format json --pretty --output "$DiffPath"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Diff failed with exit code $LASTEXITCODE." -ForegroundColor Red
        exit $LASTEXITCODE
    }

    $Diff = Get-Content -LiteralPath $DiffPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Added = [int]$Diff.summary.added
    $Removed = [int]$Diff.summary.removed
    $Changed = [int]$Diff.summary.changed

    Write-Host "Diff summary:"
    Write-Host "  Added:   $Added"
    Write-Host "  Removed: $Removed"
    Write-Host "  Changed: $Changed"
    Write-Host "  Diff:    $DiffPath"
}
else {
    Write-Host ""
    Write-Host "No previous latest.json found; skipped diff for this first run."
}

exit 0
