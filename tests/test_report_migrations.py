"""Regression tests for API migrations: never silently truncate financial data."""
import asyncio
import json
from unittest.mock import AsyncMock

from core.paginate import fetch_all
from core.registry import EndpointSpec
from wb_mcp import server as wb
from ozon_mcp import server as ozon


class Pages:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def call_spec(self, spec, **kwargs):
        from copy import deepcopy
        self.requests.append(deepcopy(kwargs))
        return {"ok": True, "status": 200, "data": next(self.responses)}


def test_wb_finance_continues_after_short_page_until_empty():
    spec = wb.catalog.get("wb_report_realization")
    assert (spec.host, spec.method, spec.path) == (
        "finance-api.wildberries.ru", "POST", "/api/finance/v1/sales-reports/detailed")
    pages = Pages([[{"rrdId": 7}], [{"rrdId": 9}], None])
    result = asyncio.run(fetch_all(pages, spec, base_body={"rrdId": 0}, limit=1000))
    assert result["total_fetched"] == 2
    assert [r["json_body"]["rrdId"] for r in pages.requests] == [0, 7, 9]


def test_order_feed_keeps_snapshot_and_does_not_mutate_input():
    spec = wb.catalog.get("wb_order_feed")
    body = {"pagination": {"offset": 0, "limit": 100}}
    pages = Pages([{"data": {"orders": [{"srid": "a"}], "snapshotTime": "stable"}},
                   {"data": {"orders": [{"srid": "b"}], "snapshotTime": "stable"}},
                   {"data": {"orders": [], "snapshotTime": "stable"}}])
    result = asyncio.run(fetch_all(pages, spec, base_body=body))
    assert result["total_fetched"] == 2
    assert [r["json_body"]["pagination"]["offset"] for r in pages.requests] == [0, 1, 2]
    assert pages.requests[1]["json_body"]["pagination"]["snapshotTime"] == "stable"
    assert body == {"pagination": {"offset": 0, "limit": 100}}


def test_ozon_accrual_cursor_has_no_old_page_or_limit():
    spec = ozon.catalog.get("ozon_finance_transactions")
    assert spec.path == "/v1/finance/accrual/by-day"
    pages = Pages([{"accruals": [{"accrual_id": "a"}], "last_id": "next"},
                   {"accruals": [{"accrual_id": "b"}], "last_id": ""}])
    result = asyncio.run(fetch_all(pages, spec, base_body={"date": "2026-10-19", "last_id": ""}))
    assert result["total_fetched"] == 2
    assert pages.requests[1]["json_body"] == {"date": "2026-10-19", "last_id": "next"}


def test_old_wb_finance_query_is_migrated_before_request(monkeypatch):
    request = AsyncMock(return_value={"ok": True, "data": []})
    monkeypatch.setattr(wb.client, "request", request)
    asyncio.run(wb.client.call_spec(wb.catalog.get("wb_report_realization"),
        query={"dateFrom": "2026-10-01", "dateTo": "2026-10-20", "rrdid": 3}))
    kwargs = request.call_args.kwargs
    assert kwargs["query"] is None
    assert kwargs["json_body"]["rrdId"] == 3


def test_old_ozon_filter_is_rejected_with_migration_hint(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(ozon.client, "request", request)
    result = asyncio.run(ozon.client.call_spec(ozon.catalog.get("ozon_finance_transactions"),
                                               json_body={"filter": {"date": {}}}))
    assert not result["ok"]
    assert "date" in result["message"]
    request.assert_not_called()


def test_order_feed_rejects_dates_without_timezone(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(wb.client, "call_spec", call)
    result = json.loads(asyncio.run(wb.wb_get_order_feed("2026-10-01", "2026-10-20")))
    assert not result["ok"]
    call.assert_not_called()


def test_realization_tool_preserves_new_schema_and_money(monkeypatch):
    call = AsyncMock(return_value={"ok": True, "data": [{"forPay": "12.34", "spp": 20}]})
    monkeypatch.setattr(wb.client, "call_spec", call)
    result = json.loads(asyncio.run(wb.wb_get_finance_report("2026-10-01", "2026-10-20")))
    assert result["data"][0]["forPay"] == "12.34"
    assert result["data"][0]["spp"] == 20
