"""Deep-analytics reads: catalog classification and the retired-metrics guard."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from core.client import MarketplaceClient, ServiceConfig
from core.registry import Catalog
from core.tools import register_generic_tools
from ozon_mcp import server as ozon

OZON_CATALOG = Path(__file__).resolve().parents[1] / "ozon_mcp" / "endpoints.yaml"


@pytest.mark.parametrize("operation_id,path", [
    ("ozon_get_product_queries", "/v1/analytics/product-queries"),
    ("ozon_post_v1_analytics_product_queries_details", "/v1/analytics/product-queries/details"),
    ("ozon_post_v1_seller_actions_products_candidates", "/v1/seller-actions/products/candidates"),
])
def test_analytics_post_is_available_to_read_executor(monkeypatch, operation_id, path):
    catalog = Catalog.from_yaml(OZON_CATALOG)
    spec = catalog.get(operation_id)
    assert (spec.method, spec.path, spec.safety) == ("POST", path, "read")
    client = MarketplaceClient(ServiceConfig(
        name="ozon", scheme="https", fields=[], env_map={},
        build_headers=lambda c: {}, allowed_host_suffixes=[".ozon.ru"]))
    call = AsyncMock(return_value={"ok": True, "data": {}})
    monkeypatch.setattr(client, "call_spec", call)
    mcp = FastMCP("analytics-read-test")
    register_generic_tools(mcp, svc="ozon", client=client, catalog=catalog)
    body = {"skus": ["5943641112"]}
    result = asyncio.run(mcp.call_tool("ozon_call_method", {"operation_id": operation_id, "body": body}))
    assert json.loads(result[0][0].text)["ok"] is True
    call.assert_awaited_once_with(spec, path_values=None, query=None, json_body=body)


def test_retired_funnel_metrics_are_rejected_before_the_request(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(ozon.client, "request", request)
    body = {"date_from": "2026-10-08", "date_to": "2026-10-20", "dimension": ["day"],
            "metrics": ["ordered_units", "hits_view_search", "revenue", "session_view"]}
    result = asyncio.run(ozon.client.call_spec(ozon.catalog.get("ozon_analytics_data"), json_body=body))
    assert result["ok"] is False
    assert result["details"]["retired_metrics"] == ["hits_view_search", "session_view"]
    request.assert_not_called()


def test_live_analytics_metrics_still_go_through(monkeypatch):
    request = AsyncMock(return_value={"ok": True, "data": {"result": {"data": []}}})
    monkeypatch.setattr(ozon.client, "request", request)
    body = {"date_from": "2026-10-08", "date_to": "2026-10-20", "dimension": ["sku", "day"],
            "metrics": ["ordered_units", "revenue"]}
    result = asyncio.run(ozon.client.call_spec(ozon.catalog.get("ozon_analytics_data"), json_body=body))
    assert result["ok"] is True
    assert request.call_args.kwargs["json_body"] == body
