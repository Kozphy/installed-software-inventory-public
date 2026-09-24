"""
Package root for Installed Software Inventory.

Exposes the public package version used by the CLI ``--version`` flag and
distribution metadata. Application logic lives in sibling modules that form
the scan pipeline:

    collectors (registry + appx) → normalize → report → exporters
                                                ↘ diff (snapshot comparison)
                                                ↘ winget_bridge (reinstall companion)

This package is intentionally local-first and read-only: it never phones home
and never modifies the Windows Registry, packages, or uninstalls software.
"""

from __future__ import annotations

__version__ = "1.3.0"
__all__ = ["__version__"]
