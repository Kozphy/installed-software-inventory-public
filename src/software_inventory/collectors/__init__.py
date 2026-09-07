"""
Collectors package: entry point for local software discovery.

Re-exports the Windows Uninstall Registry collector so the CLI and other
callers can import from ``software_inventory.collectors`` without knowing
which OS-specific backend is active.

Pipeline role:
    Input  → live Registry hives (or injected test doubles)
    Output → raw ``SoftwareEntry`` lists for ``normalize`` / ``report``
"""

from __future__ import annotations

from software_inventory.collectors.windows_registry import (
    collect_from_registry,
    describe_collector_sources,
)

__all__ = ["collect_from_registry", "describe_collector_sources"]
