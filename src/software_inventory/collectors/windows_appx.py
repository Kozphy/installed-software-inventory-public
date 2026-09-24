"""
Appx / MSIX package collector (read-only discovery of Store and packaged apps).

Complements the Uninstall Registry collector: Microsoft Store apps and other
MSIX packages never write Uninstall keys, so they were invisible before v1.3.

    powershell.exe (Windows PowerShell 5.1) → WinRT PackageManager
        → FindPackagesForUser("") as compact JSON on stdout
        → parse_appx_packages → entry_from_appx_package
        → raw SoftwareEntry list tagged ``source="appx"``

Why this shape:
    * ``Windows.Management.Deployment.PackageManager`` returns *localized*
      display names (``Get-AppxPackage`` only exposes the package identity
      name such as ``Microsoft.WindowsTerminal``).
    * WinRT type projection only exists in Windows PowerShell 5.1, not in
      PowerShell 7 (``pwsh``), so the collector always targets
      ``powershell.exe``.
    * The script is passed with ``-EncodedCommand`` so no quoting survives
      into the command line, and it never takes user input. Its stdout is
      ASCII-only JSON, so the console code page cannot mangle names.

Safety product choices:
    * Enumeration only — never calls Add/Remove/Register package APIs.
    * Honors ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN`` like the Registry collector.
    * Resource packages (language/scale satellites) are dropped; frameworks
      and OS-signed packages are kept but flagged ``system_component`` so the
      default export hides them.
"""


from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from software_inventory.collectors.windows_registry import live_scan_disabled
from software_inventory.models import SOURCE_APPX, SoftwareEntry
from software_inventory.normalize import normalize_install_date, normalize_string

logger = logging.getLogger(__name__)

APPX_QUERY_TIMEOUT_SECONDS = 120
APPX_REGISTRY_PATH_PREFIX = "appx:"
APPX_SOURCE_LABEL = (
    "Appx/MSIX packages [Windows.Management.Deployment.PackageManager, current user]"
)

APPX_QUERY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[void][Windows.Management.Deployment.PackageManager, Windows.Management.Deployment, ContentType = WindowsRuntime]
$pm = New-Object Windows.Management.Deployment.PackageManager
$rows = @(foreach ($p in $pm.FindPackagesForUser('')) {
  $displayName = $null; $publisherName = $null; $location = $null; $installed = $null
  try { $displayName = $p.DisplayName } catch {}
  try { $publisherName = $p.PublisherDisplayName } catch {}
  try { $location = $p.InstalledLocation.Path } catch {}
  try { $installed = $p.InstalledDate.ToString('o') } catch {}
  $v = $p.Id.Version
  [pscustomobject]@{
    name = $p.Id.Name
    family = $p.Id.FamilyName
    full_name = $p.Id.FullName
    display_name = $displayName
    publisher_display_name = $publisherName
    publisher = $p.Id.Publisher
    version = '{0}.{1}.{2}.{3}' -f $v.Major, $v.Minor, $v.Build, $v.Revision
    architecture = [string]$p.Id.Architecture
    install_location = $location
    installed_date = $installed
    is_framework = [bool]$p.IsFramework
    is_resource = [bool]$p.IsResourcePackage
    is_bundle = [bool]$p.IsBundle
    signature_kind = [string]$p.SignatureKind
  }
})
$json = ConvertTo-Json -InputObject $rows -Depth 3 -Compress
[regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
"""
"""Enumeration-only script; kept as a constant so it can be reviewed and tested.

The final line escapes every non-ASCII UTF-16 unit as ``\\uXXXX`` because
Windows PowerShell 5.1 encodes redirected stdout with the console code page
(e.g. cp950), which would otherwise corrupt localized display names.
"""

_ARCHITECTURE_LABELS: dict[str, str] = {
    "x64": "64-bit",
    "x86": "32-bit",
    "x86onarm64": "32-bit",
    "arm64": "arm64",
    "arm": "arm",
    "neutral": "neutral",
}

_SUBJECT_ORG = re.compile(r"(?:^|,)\s*O=\"?([^,\"]+)")
_SUBJECT_CN = re.compile(r"(?:^|,)\s*CN=\"?([^,\"]+)")
_CLIXML_ERROR = re.compile(r'<S S="Error">(.*?)</S>', re.DOTALL)

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def resolve_powershell_executable() -> str:
    """
    Locate Windows PowerShell 5.1, which provides the WinRT type projection.

    Returns:
        str: Absolute path to ``powershell.exe``.

    Raises:
        FileNotFoundError: When Windows PowerShell is not installed.
    """
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    if found:
        return found
    raise FileNotFoundError(
        "Windows PowerShell (powershell.exe) was not found; "
        "it is required to list Appx/MSIX packages"
    )


def encode_powershell_command(script: str) -> str:
    """
    Encode a script for ``powershell.exe -EncodedCommand``.

    Args:
        script (str): PowerShell source text.

    Returns:
        str: Base64 of the UTF-16LE script bytes.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _summarize_stderr(raw: str, limit: int = 300) -> str:
    """
    Reduce PowerShell stderr (often CLIXML-wrapped) to a short readable message.

    Args:
        raw (str): Decoded stderr text.
        limit (int): Maximum characters to keep.

    Returns:
        str: Error text with CLIXML progress records removed.
    """
    text = raw.strip()
    if text.startswith("#< CLIXML"):
        errors = _CLIXML_ERROR.findall(text)
        text = " ".join(errors).replace("_x000D__x000A_", " ").strip()
    text = " ".join(text.split())
    return text[:limit]


def run_appx_query(
    *,
    runner: Optional[Runner] = None,
    executable: Optional[str] = None,
    timeout: int = APPX_QUERY_TIMEOUT_SECONDS,
) -> str:
    """
    Run the enumeration script and return its JSON stdout.

    Args:
        runner (Runner | None): ``subprocess.run`` stand-in for tests.
        executable (str | None): PowerShell path override; resolved when None.
        timeout (int): Seconds before the query is abandoned.

    Returns:
        str: UTF-8 JSON text (array of package objects).

    Raises:
        FileNotFoundError: Windows PowerShell is missing.
        RuntimeError: Non-zero exit or timeout.
    """
    run = runner if runner is not None else subprocess.run
    exe = executable if executable is not None else resolve_powershell_executable()
    argv = [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encode_powershell_command(APPX_QUERY_SCRIPT),
    ]
    kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout, "check": False}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        completed = run(argv, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Appx package query timed out after {timeout}s") from exc

    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    stdout_text = stdout.decode("utf-8-sig", errors="replace") if isinstance(stdout, bytes) else str(stdout)
    stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)

    if completed.returncode != 0:
        detail = _summarize_stderr(stderr_text) or "no error output"
        raise RuntimeError(
            f"Appx package query failed (exit {completed.returncode}): {detail}"
        )
    return stdout_text


def parse_appx_packages(text: str) -> list[dict[str, Any]]:
    """
    Parse the enumeration script's JSON into package dictionaries.

    Args:
        text (str): Script stdout. A single object (older ``ConvertTo-Json``
            behavior) is accepted as a one-element list; blank output is empty.

    Returns:
        list[dict[str, Any]]: Package objects; non-object items are skipped.

    Raises:
        ValueError: Output is not valid JSON or has an unexpected shape.
    """
    stripped = text.strip().lstrip("\ufeff")
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Appx package query returned invalid JSON: {exc}") from exc
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise ValueError("Appx package query returned JSON that is not an array of objects")


def _as_bool(value: Any) -> bool:
    """
    Interpret JSON booleans and their string spellings.

    Args:
        value (Any): Raw JSON value.

    Returns:
        bool: True for ``true`` / ``1`` / ``yes`` style values.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes"}


def publisher_from_subject(subject: Optional[str]) -> Optional[str]:
    """
    Derive a readable publisher from a package signing subject.

    Args:
        subject (str | None): Distinguished name such as
            ``CN=Microsoft Windows, O=Microsoft Corporation, C=US``.

    Returns:
        str | None: ``O=`` value, else ``CN=`` value, else None.
    """
    text = normalize_string(subject)
    if text is None:
        return None
    for pattern in (_SUBJECT_ORG, _SUBJECT_CN):
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def architecture_label(value: Optional[str]) -> str:
    """
    Map ``ProcessorArchitecture`` names onto inventory architecture labels.

    Args:
        value (str | None): ``X64``, ``X86``, ``Arm64``, ``Neutral``, ...

    Returns:
        str: ``64-bit`` / ``32-bit`` (matching Registry rows), ``arm64``,
        ``arm``, ``neutral``, or ``unknown``.
    """
    key = (normalize_string(value) or "").lower()
    return _ARCHITECTURE_LABELS.get(key, "unknown")


def entry_from_appx_package(package: Mapping[str, Any]) -> Optional[SoftwareEntry]:
    """
    Map one enumerated package onto the shared ``SoftwareEntry`` model.

    Args:
        package (Mapping[str, Any]): One object from ``parse_appx_packages``.

    Returns:
        SoftwareEntry | None: Normalized row, or None for resource packages,
        unresolved ``ms-resource:`` names, and objects without an identity.

    Notes:
        ``registry_path`` is ``appx:<PackageFamilyName>`` because the family
        name survives upgrades, unlike the versioned install folder.
        Uninstall strings stay empty: this tool never suggests removal
        commands for packages.
    """
    if _as_bool(package.get("is_resource")):
        return None

    package_name = normalize_string(package.get("name"))
    family = normalize_string(package.get("family"))
    if not package_name and not family:
        return None

    display_name = normalize_string(package.get("display_name"))
    if display_name and display_name.lower().startswith("ms-resource:"):
        display_name = None
    name = display_name or package_name
    if not name:
        return None

    installed = normalize_string(package.get("installed_date"))
    signature_kind = (normalize_string(package.get("signature_kind")) or "").lower()

    return SoftwareEntry(
        name=name,
        version=normalize_string(package.get("version")),
        publisher=normalize_string(package.get("publisher_display_name"))
        or publisher_from_subject(package.get("publisher")),
        install_date=normalize_install_date(installed[:10]) if installed else None,
        install_location=normalize_string(package.get("install_location")),
        estimated_size_kb=None,
        scope="current_user",
        architecture=architecture_label(package.get("architecture")),
        uninstall_string=None,
        quiet_uninstall_string=None,
        registry_path=f"{APPX_REGISTRY_PATH_PREFIX}{family or package_name}",
        release_type=None,
        system_component=_as_bool(package.get("is_framework")) or signature_kind == "system",
        source=SOURCE_APPX,
    )


def describe_appx_source() -> str:
    """
    Human-readable label for ``scan.collector_sources``.

    Returns:
        str: Stable description of the Appx enumeration source.
    """
    return APPX_SOURCE_LABEL


def collect_from_appx(*, runner: Optional[Runner] = None) -> list[SoftwareEntry]:
    """
    Enumerate Appx/MSIX packages for the current user and return **raw** entries.

    Args:
        runner (Runner | None): Injected ``subprocess.run`` stand-in for tests.
            When omitted, runs the live PowerShell query on Windows.

    Returns:
        list[SoftwareEntry]: Rows tagged ``source="appx"`` (unfiltered).

    Raises:
        RuntimeError: Live scan disabled via ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN``,
            or the PowerShell query failed / timed out.
        OSError: Host is not Windows, or PowerShell is missing.
        ValueError: Query output was not valid JSON.
    """
    if runner is None:
        if live_scan_disabled():
            raise RuntimeError(
                "live Appx scan is disabled because "
                "SOFTWARE_INVENTORY_SKIP_LIVE_SCAN is set"
            )
        if sys.platform != "win32":
            raise OSError(
                "Appx/MSIX enumeration only runs on Windows. "
                f"Detected platform: {sys.platform!r}."
            )

    text = run_appx_query(runner=runner, executable="powershell.exe" if runner else None)
    entries: list[SoftwareEntry] = []
    for package in parse_appx_packages(text):
        entry = entry_from_appx_package(package)
        if entry is not None:
            entries.append(entry)
    logger.info("Appx scan collected %d raw entries", len(entries))
    return entries


__all__ = [
    "APPX_QUERY_SCRIPT",
    "APPX_REGISTRY_PATH_PREFIX",
    "APPX_SOURCE_LABEL",
    "architecture_label",
    "collect_from_appx",
    "describe_appx_source",
    "encode_powershell_command",
    "entry_from_appx_package",
    "parse_appx_packages",
    "publisher_from_subject",
    "resolve_powershell_executable",
    "run_appx_query",
]
