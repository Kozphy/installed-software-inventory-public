"""Read installed software from Windows Uninstall Registry keys.

This module is intentionally read-only: it never writes Registry values and
never invokes uninstall commands. It does not use Win32_Product.
"""

from __future__ import annotations

import logging
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
    """Describes one Uninstall Registry hive/view to scan."""

    hive_name: str
    subkey: str
    scope: str
    architecture: str
    access_name: str


def _require_windows() -> None:
    """Raise ``OSError`` when the host is not Windows."""
    if sys.platform != "win32":
        raise OSError(
            "Installed Software Inventory only runs on Windows. "
            f"Detected platform: {sys.platform!r}. "
            "The tool reads Windows Registry Uninstall keys and cannot operate "
            "on other operating systems."
        )


def _load_winreg() -> Any:
    """Import ``winreg`` or raise a clear error on non-Windows platforms."""
    _require_windows()
    import winreg  # noqa: PLC0415 — platform-gated import

    return winreg


def _registry_sources(winreg: Any) -> list[tuple[Any, RegistrySource]]:
    """Return (hive_constant, source) pairs covering 32/64-bit and HKCU views."""
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
    """Resolve a named Registry view access mask."""
    base = winreg.KEY_READ
    if access_name == "KEY_WOW64_64KEY":
        return base | getattr(winreg, "KEY_WOW64_64KEY", 0)
    if access_name == "KEY_WOW64_32KEY":
        return base | getattr(winreg, "KEY_WOW64_32KEY", 0)
    return base


def _read_value(key: Any, name: str, winreg: Any) -> object | None:
    """Read a Registry value, returning ``None`` when missing or unreadable."""
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
    """Build a ``SoftwareEntry`` from a flat value map."""
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
    """Read the Uninstall value names we care about from an open key."""
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
    """List immediate subkey names under an open Registry key."""
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
    """Collect software entries from a single Registry source."""
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
    """Return human-readable labels for Registry sources that may be scanned."""
    return [
        f"HKEY_LOCAL_MACHINE\\{UNINSTALL_SUBKEY} [64-bit view]",
        f"HKEY_LOCAL_MACHINE\\{UNINSTALL_SUBKEY} [32-bit view]",
        f"HKEY_LOCAL_MACHINE\\{WOW64_UNINSTALL_SUBKEY}",
        f"HKEY_CURRENT_USER\\{UNINSTALL_SUBKEY}",
    ]


def collect_from_registry(*, winreg_module: Any | None = None) -> list[SoftwareEntry]:
    """Scan supported Uninstall Registry locations and return raw entries.

    Parameters
    ----------
    winreg_module:
        Optional stand-in for the ``winreg`` module (used by tests).
    """
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
    "_entry_from_values",
]
