"""Combined server (.mcpb / `uvx marketplaces-mcp-ru`) mounts every service.

The combined FastMCP is built by copying each service's already-registered
tools via FastMCP's internal tool manager (``_tool_manager._tools``). That
surface is private, so this test locks it: if an ``mcp`` upgrade moves it, the
combined tool count drops and this fails loudly instead of shipping an empty
Claude Desktop bundle. Tool names are namespaced per service, so the union is
exact — no collisions, nothing dropped.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import combined  # noqa: E402


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_combined_is_exact_union_of_services():
    import importlib

    per_service: set[str] = set()
    for mod_name in combined.SERVICE_MODULES:
        mod = importlib.import_module(mod_name)
        names = _tool_names(mod.mcp)
        assert names, f"{mod_name} exposed no tools"
        # namespaced prefixes guarantee disjoint sets
        assert not (per_service & names), f"tool name collision from {mod_name}"
        per_service |= names

    got = _tool_names(combined.build())
    per_service.add("telegram_send")
    from core.analytics_addon import TOOLS
    per_service.update(name for name, _ in TOOLS)
    assert got == per_service, (
        "combined server is not the exact union of the service tools "
        f"(missing: {per_service - got}, extra: {got - per_service})"
    )
