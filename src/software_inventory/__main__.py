"""Allow ``python -m software_inventory`` entry point."""

from __future__ import annotations

from software_inventory.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
