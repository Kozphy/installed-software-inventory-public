"""Collectors that gather installed-software records from the local system."""

from __future__ import annotations

from software_inventory.collectors.windows_registry import collect_from_registry

__all__ = ["collect_from_registry"]
