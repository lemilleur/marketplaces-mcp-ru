"""Supply list/details POST endpoints must work through read executors."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from core.client import MarketplaceClient, ServiceConfig
from core.registry import Catalog
from core.tools import register_generic_tools


@pytest.mark.parametrize("svc,operation_id,host,path", [
    ("wb", "wb_post_api_supplies", "supplies-api.wildberries.ru", "/api/v1/supplies"),
    ("ozon", "ozon_post_v1_supply_order_details", "api-seller.ozon.ru", "/v1/supply-order/details"),
])
def test_supply_post_is_available_to_read_executor(monkeypatch, svc, operation_id, host, path):
    catalog = Catalog.from_yaml(Path(__file__).resolve().parents[1] / f"{svc}_mcp" / "endpoints.yaml")
    spec = catalog.get(operation_id)
    assert (spec.method, spec.host, spec.path, spec.safety) == ("POST", host, path, "read")
    client = MarketplaceClient(ServiceConfig(
        name=svc, scheme="https", fields=[], env_map={},
        build_headers=lambda c: {}, allowed_host_suffixes=[".wildberries.ru", ".ozon.ru"]))
    call = AsyncMock(return_value={"ok": True, "data": {"supplies": []}})
    monkeypatch.setattr(client, "call_spec", call)
    mcp = FastMCP("supply-read-test")
    register_generic_tools(mcp, svc=svc, client=client, catalog=catalog)
    body = {"test_filter": 1}
    result = asyncio.run(mcp.call_tool(f"{svc}_call_method", {"operation_id": operation_id, "body": body}))
    assert json.loads(result[0][0].text)["ok"] is True
    call.assert_awaited_once_with(spec, path_values=None, query=None, json_body=body)
