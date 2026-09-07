"""
Module entry point for ``python -m software_inventory``.

Delegates to :func:`software_inventory.cli.main` so the package can be invoked
without installing a console script. Exit codes follow the CLI contract
(0 success, 1 runtime, 2 usage, 3 unsupported platform).
"""

from __future__ import annotations

from software_inventory.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
