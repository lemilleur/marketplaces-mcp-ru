"""Compatibility shim: `python -m core.analytics_addon snapshot` keeps working.

The analytics tools moved into the regular services (through MarketplaceClient,
the catalog and the safety gate) and snapshots into core.snapshots. Cron jobs
set up with the old command are forwarded there; switch them to
`python -m core.snapshots` when convenient.
"""
from __future__ import annotations

import sys

from core.snapshots import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["snapshot"]:
        args = args[1:]
    print("[marketplace-mcp] core.analytics_addon is deprecated: "
          "use `python -m core.snapshots`", file=sys.stderr)
    sys.exit(main(args))
