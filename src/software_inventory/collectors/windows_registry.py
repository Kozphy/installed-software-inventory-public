"""
Windows Uninstall Registry collector (read-only discovery).

First stage of a live scan: enumerate traditional Uninstall keys and emit raw
``SoftwareEntry`` rows for prepare/export.

    HKLM 64/32-bit views + WOW6432Node + HKCU
        → raw SoftwareEntry list (unfiltered, may contain duplicates)
        → normalize.prepare_inventory* → report/export

Safety product choices:
    * Never writes Registry values or runs uninstall commands.
    * Avoids WMI ``Win32_Product`` (queries can trigger Windows Installer repair).
    * Standard-user readable keys only; protected keys are skipped quietly.
    * Honors ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN`` so CI/harness cannot touch
      the live hive.

Coverage limits (by design of Uninstall keys, not bugs):
    Microsoft Store / MSIX apps, portable binaries, and some package-manager
    installs may never appear. Empty ``DisplayName`` subkeys are dropped when
    mapped to ``SoftwareEntry``.
"""


from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

from software_inventory.models import SoftwareEntry
from software_inventory.normalize import (
    normalize_estimated_size_kb,
    normalize_install_date,
    normalize_string,
    normalize_system_component,
)

logger = logging.getLogger(__name__)

UNINSTALL_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
WOW64_UNINSTALL_SUBKEY = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"


@dataclass(frozen=True)
class RegistrySource:
    """
    Describes one Uninstall Registry hive/view to scan.

    Responsibilities:
        * Capture hive path, install scope, architecture label, and access mask
          name so ``collect_source`` can open the correct 32/64-bit view.
    """

    hive_name: str
    subkey: str
    scope: str
    architecture: str
    access_name: str


def live_scan_disabled() -> bool:
    """
    Report whether the environment forbids touching the live Registry.

    Used by CI and the CLI harness so process-level tests never query Uninstall
    keys on the runner machine.

    Returns:
        bool: True when ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN`` is a truthy
        token (``1``, ``true``, ``yes``, ``on``).
    """
    value = os.environ.get("SOFTWARE_INVENTORY_SKIP_LIVE_SCAN", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_windows() -> None:
    """
    Guard live Registry access so non-Windows hosts fail with a clear error.

    Raises:
        OSError: When ``sys.platform`` is not ``win32``.
    """
    if sys.platform != "win32":
        raise OSError(
            "Installed Software Inventory only runs on Windows. "
            f"Detected platform: {sys.platform!r}. "
            "The tool reads Windows Registry Uninstall keys and cannot operate "
            "on other operating systems."
        )


def _load_winreg() -> Any:
    """
    Import the stdlib ``winreg`` module after verifying the host is Windows.

    Returns:
        Any: The imported ``winreg`` module.

    Raises:
        OSError: When the platform is not Windows (via ``_require_windows``).
    """
    _require_windows()
    import winreg  # noqa: PLC0415 — platform-gated import

    return winreg


def _registry_sources(winreg: Any) -> list[tuple[Any, RegistrySource]]:
    """
    Ordered Uninstall hives/views scanned on each live run.

    Includes both the 32-bit Registry *view* of HKLM\\...\\Uninstall and the
    ``WOW6432Node\\...\\Uninstall`` path. Those can overlap; prepare-time
    dedupe is what collapses duplicates afterward.

    Args:
        winreg (Any): The ``winreg`` module (or test double).

    Returns:
        list[tuple[Any, RegistrySource]]: (hive constant, source metadata) pairs.
        No elevation is required for the default readable set.
    """
    sources: list[tuple[Any, RegistrySource]] = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            RegistrySource(
                hive_name="HKEY_LOCAL_MACHINE",
                subkey=UNINSTALL_SUBKEY,
                scope="machine",
                architecture="64-bit",
                access_name="KEY_WOW64_64KEY",
            ),
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            RegistrySource(
                hive_name="HKEY_LOCAL_MACHINE",
                subkey=UNINSTALL_SUBKEY,
                scope="machine",
                architecture="32-bit",
                access_name="KEY_WOW64_32KEY",
            ),
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            RegistrySource(
                hive_name="HKEY_LOCAL_MACHINE",
                subkey=WOW64_UNINSTALL_SUBKEY,
                scope="machine",
                architecture="32-bit",
                access_name="KEY_WOW64_64KEY",
            ),
        ),
        (
            winreg.HKEY_CURRENT_USER,
            RegistrySource(
                hive_name="HKEY_CURRENT_USER",
                subkey=UNINSTALL_SUBKEY,
                scope="current_user",
                architecture="unknown",
                access_name="DEFAULT",
            ),
        ),
    ]
    return sources


def _access_mask(winreg: Any, access_name: str) -> int:
    """
    Resolve a named Registry view access mask for KEY_READ.

    Args:
        winreg (Any): The ``winreg`` module (or test double).
        access_name (str): ``KEY_WOW64_64KEY``, ``KEY_WOW64_32KEY``, or
            ``DEFAULT`` for the process native view.

    Returns:
        int: Combined access flags for ``OpenKey``.
    """
    base = winreg.KEY_READ
    if access_name == "KEY_WOW64_64KEY":
        return base | getattr(winreg, "KEY_WOW64_64KEY", 0)
    if access_name == "KEY_WOW64_32KEY":
        return base | getattr(winreg, "KEY_WOW64_32KEY", 0)
    return base


def _read_value(key: Any, name: str, winreg: Any) -> object | None:
    """
    Read one Registry value without failing the whole scan on missing data.

    Args:
        key (Any): Open Registry key handle.
        name (str): Value name such as ``DisplayName``.
        winreg (Any): The ``winreg`` module (or test double).

    Returns:
        object | None: Raw Registry value, or None when missing/unreadable.
    """
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None
    except Exception:  # pragma: no cover - defensive
        logger.debug("Unexpected error reading Registry value %r", name, exc_info=True)
        return None


def _entry_from_values(
    values: dict[str, object | None],
    *,
    registry_path: str,
    scope: str,
    architecture: str,
) -> Optional[SoftwareEntry]:
    """
    Map a flat Uninstall value dictionary into a ``SoftwareEntry``.

    Entries without a usable ``DisplayName`` are dropped; incomplete optional
    fields become None/False after normalization.

    Args:
        values (dict[str, object | None]): Raw Uninstall value map.
        registry_path (str): Full source path for provenance and debug.
        scope (str): ``machine`` or ``current_user``.
        architecture (str): Architecture label for this Registry view.

    Returns:
        SoftwareEntry | None: Normalized entry, or None if DisplayName is empty.
    """
    name = normalize_string(values.get("DisplayName"))
    if not name:
        return None

    return SoftwareEntry(
        name=name,
        version=normalize_string(values.get("DisplayVersion")),
        publisher=normalize_string(values.get("Publisher")),
        install_date=normalize_install_date(values.get("InstallDate")),
        install_location=normalize_string(values.get("InstallLocation")),
        estimated_size_kb=normalize_estimated_size_kb(values.get("EstimatedSize")),
        scope=scope,
        architecture=architecture,
        uninstall_string=normalize_string(values.get("UninstallString")),
        quiet_uninstall_string=normalize_string(values.get("QuietUninstallString")),
        registry_path=registry_path,
        release_type=normalize_string(values.get("ReleaseType")),
        system_component=normalize_system_component(values.get("SystemComponent")),
    )


def _read_subkey_values(key: Any, winreg: Any) -> dict[str, object | None]:
    """
    Read the Uninstall value names used by the inventory model.

    Args:
        key (Any): Open application Uninstall subkey.
        winreg (Any): The ``winreg`` module (or test double).

    Returns:
        dict[str, object | None]: Name → raw value (or None if absent).
    """
    names = (
        "DisplayName",
        "DisplayVersion",
        "Publisher",
        "InstallDate",
        "InstallLocation",
        "EstimatedSize",
        "UninstallString",
        "QuietUninstallString",
        "ReleaseType",
        "SystemComponent",
    )
    return {name: _read_value(key, name, winreg) for name in names}


def _enumerate_subkeys(key: Any, winreg: Any) -> list[str]:
    """
    List immediate child subkey names under an open Registry key.

    Args:
        key (Any): Open parent Registry key.
        winreg (Any): The ``winreg`` module (or test double).

    Returns:
        list[str]: Subkey names in enumeration order.
    """
    names: list[str] = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
            index += 1
        except OSError:
            break
    return names


def collect_source(
    hive: Any,
    source: RegistrySource,
    winreg: Any,
) -> list[SoftwareEntry]:
    """
    Collect software entries from a single Uninstall Registry source.

    Unreadable keys are skipped quietly so a locked or partial hive does not
    abort the rest of the scan.

    Args:
        hive (Any): Hive constant such as ``winreg.HKEY_LOCAL_MACHINE``.
        source (RegistrySource): Path/scope/architecture/access metadata.
        winreg (Any): The ``winreg`` module (or test double).

    Returns:
        list[SoftwareEntry]: Entries discovered under this source (may be empty).
    """
    access = _access_mask(winreg, source.access_name)
    entries: list[SoftwareEntry] = []

    try:
        root = winreg.OpenKey(hive, source.subkey, 0, access)
    except OSError as exc:
        logger.debug(
            "Unable to open %s\\%s (%s): %s",
            source.hive_name,
            source.subkey,
            source.access_name,
            exc,
        )
        return entries

    try:
        for sub_name in _enumerate_subkeys(root, winreg):
            registry_path = f"{source.hive_name}\\{source.subkey}\\{sub_name}"
            try:
                with winreg.OpenKey(root, sub_name, 0, access) as app_key:
                    values = _read_subkey_values(app_key, winreg)
            except OSError as exc:
                logger.debug("Skipping unreadable key %s: %s", registry_path, exc)
                continue

            entry = _entry_from_values(
                values,
                registry_path=registry_path,
                scope=source.scope,
                architecture=source.architecture,
            )
            if entry is not None:
                entries.append(entry)
                logger.debug("Collected: %s (%s)", entry.name, registry_path)
    finally:
        try:
            winreg.CloseKey(root)
        except OSError:
            pass

    logger.debug(
        "Source %s\\%s [%s/%s] yielded %d entries",
        source.hive_name,
        source.subkey,
        source.scope,
        source.architecture,
        len(entries),
    )
    return entries


def describe_collector_sources() -> list[str]:
    """
    List human-readable labels for Registry sources that may be scanned.

    Embedded in v1.1 report ``scan.collector_sources`` so consumers can see
    which hives contributed without re-running the collector.

    Returns:
        list[str]: Stable source labels in scan order.
    """
    return [
        f"HKEY_LOCAL_MACHINE\\{UNINSTALL_SUBKEY} [64-bit view]",
        f"HKEY_LOCAL_MACHINE\\{UNINSTALL_SUBKEY} [32-bit view]",
        f"HKEY_LOCAL_MACHINE\\{WOW64_UNINSTALL_SUBKEY}",
        f"HKEY_CURRENT_USER\\{UNINSTALL_SUBKEY}",
    ]


def collect_from_registry(*, winreg_module: Any | None = None) -> list[SoftwareEntry]:
    """
    Scan supported Uninstall locations and return **raw** entries.

    Primary collector entry point for the CLI live-scan path. Does not filter
    system components or updates — that policy lives in prepare so the same
    raw list can be re-exported with different flags.

    Args:
        winreg_module (Any | None): Injected ``winreg`` stand-in for tests.
            When omitted, imports the live stdlib module on Windows.

    Returns:
        list[SoftwareEntry]: Concatenation of all sources (duplicates possible).

    Raises:
        RuntimeError: Live scan disabled via ``SOFTWARE_INVENTORY_SKIP_LIVE_SCAN``
            and no ``winreg_module`` was injected (maps to CLI exit code 1).
        OSError: Host is not Windows on the live import path (CLI exit code 3).
    """
    if winreg_module is None and live_scan_disabled():
        raise RuntimeError(
            "live Registry scan is disabled because "
            "SOFTWARE_INVENTORY_SKIP_LIVE_SCAN is set"
        )
    winreg = winreg_module if winreg_module is not None else _load_winreg()
    collected: list[SoftwareEntry] = []

    for hive, source in _registry_sources(winreg):
        collected.extend(collect_source(hive, source, winreg))

    logger.info("Registry scan collected %d raw entries", len(collected))
    return collected


# Public helpers exposed for unit tests without touching the live Registry.
__all__ = [
    "RegistrySource",
    "UNINSTALL_SUBKEY",
    "WOW64_UNINSTALL_SUBKEY",
    "collect_from_registry",
    "collect_source",
    "describe_collector_sources",
    "live_scan_disabled",
    "_entry_from_values",
]
