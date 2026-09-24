"""
Collectors package: entry point for local software discovery.

Re-exports the Windows Uninstall Registry and Appx/MSIX collectors, and
``collect_inventory`` which runs the set selected by ``--source``.

Pipeline role:
    Input  → live Registry hives + Appx package catalog (or injected doubles)
    Output → raw ``SoftwareEntry`` lists for ``normalize`` / ``report``
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable, Optional

from software_inventory.collectors.windows_appx import collect_from_appx, describe_appx_source
from software_inventory.collectors.windows_registry import (
    collect_from_registry,
    describe_collector_sources,
)
from software_inventory.models import SoftwareEntry

logger = logging.getLogger(__name__)

SOURCE_CHOICES: tuple[str, ...] = ("all", "registry", "appx")
"""Values accepted by ``--source``; ``all`` is the default."""

Collector = Callable[[], list[SoftwareEntry]]


def collect_inventory(
    source: str = "all",
    *,
    registry_collector: Optional[Collector] = None,
    appx_collector: Optional[Collector] = None,
) -> tuple[list[SoftwareEntry], list[str]]:
    """
    Run the collectors selected by ``source`` and label what contributed.

    Args:
        source (str): ``all``, ``registry``, or ``appx``.
        registry_collector (Collector | None): Stand-in for tests.
        appx_collector (Collector | None): Stand-in for tests.

    Returns:
        tuple[list[SoftwareEntry], list[str]]: Raw entries plus the
        ``scan.collector_sources`` labels of collectors that succeeded.

    Raises:
        ValueError: Unknown ``source``.
        RuntimeError / OSError: From the Registry collector, or from the Appx
            collector when it is the only one requested.

    Notes:
        With ``all``, an Appx failure (PowerShell missing, policy-restricted
        host, timeout) is logged as a warning and the Registry rows are still
        returned; the Appx label is then absent from the sources list.
    """
    if source not in SOURCE_CHOICES:
        raise ValueError(f"unknown source {source!r}; expected one of {', '.join(SOURCE_CHOICES)}")

    run_registry = registry_collector or collect_from_registry
    run_appx = appx_collector or collect_from_appx
    entries: list[SoftwareEntry] = []
    labels: list[str] = []

    if source in ("all", "registry"):
        entries.extend(run_registry())
        labels.extend(describe_collector_sources())

    if source == "appx":
        entries.extend(run_appx())
        labels.append(describe_appx_source())
    elif source == "all":
        try:
            entries.extend(run_appx())
            labels.append(describe_appx_source())
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning("Appx/MSIX packages skipped: %s", exc)

    return entries, labels


__all__ = [
    "SOURCE_CHOICES",
    "collect_from_appx",
    "collect_from_registry",
    "collect_inventory",
    "describe_appx_source",
    "describe_collector_sources",
]
