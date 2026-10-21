"""Daily snapshots: stored per item, readable as history, tolerant to a failing source."""
import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP

import core.paginate
from core import snapshots
from core.snapshots import Source
from ozon_mcp import server as ozon
from wb_mcp import server as wb

# Shape of a real /v5/product/info/prices item (2026-10-21), trimmed.
KLIMT = {"offer_id": "ArtKlimtKiss", "price": {"price": 5500, "marketing_seller_price": 4200,
         "min_price": 4200, "auto_action_enabled": True},
         "marketing_actions": {"actions": [{"title": "Эластичный бустинг", "value": 4200}]}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_MCP_SNAPSHOT_DB", str(tmp_path / "s.sqlite"))
    return tmp_path / "s.sqlite"


def test_snapshot_then_history_by_field(db, monkeypatch):
    async def fake_fetch_all(client, spec, **kw):
        assert spec.operation_id == "ozon_prices_get"
        return {"ok": True, "items": [KLIMT], "total_fetched": 1}
    monkeypatch.setattr(core.paginate, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(ozon.client.config, "missing_creds", lambda: [])
    src = [Source("ozon", "prices", "ozon_mcp.server", "ozon_prices_get")]
    report = asyncio.run(snapshots.take_snapshot("2026-10-15", sources=src))
    assert report["ok"] and report["saved"] == {"ozon.prices": 1}

    mcp = FastMCP("history-test")
    snapshots.register_history_tool(mcp, svc="ozon", kinds=["prices", "stocks"])
    res = asyncio.run(mcp.call_tool("ozon_get_history", {
        "kind": "prices", "date_from": "2026-10-01", "date_to": "2026-10-31",
        "key": "ArtKlimtKiss", "fields": "price.marketing_seller_price,price.min_price"}))
    out = json.loads(res[0][0].text)
    assert out["rows"] == [{"date": "2026-10-15", "key": "ArtKlimtKiss",
                            "price.marketing_seller_price": 4200, "price.min_price": 4200}]
    assert out["days_available"] == {"first": "2026-10-15", "last": "2026-10-15", "count": 1}


def test_failing_or_keyless_source_does_not_stop_the_rest(db, monkeypatch):
    async def fake_fetch_all(client, spec, **kw):
        if spec.operation_id == "wb_prices_list":
            raise RuntimeError("boom")
        return {"ok": True, "items": [KLIMT]}
    monkeypatch.setattr(core.paginate, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(ozon.client.config, "missing_creds", lambda: [])
    monkeypatch.setattr(wb.client.config, "missing_creds", lambda: [])
    src = [Source("wb", "prices", "wb_mcp.server", "wb_prices_list"),
           Source("ozon", "prices", "ozon_mcp.server", "ozon_prices_get")]
    report = asyncio.run(snapshots.take_snapshot("2026-10-15", sources=src))
    assert "wb.prices" in report["errors"] and report["saved"] == {"ozon.prices": 1}

    monkeypatch.setattr(ozon.client.config, "missing_creds", lambda: ["api_key"])
    report = asyncio.run(snapshots.take_snapshot("2026-10-16", sources=src[1:]))
    assert report["skipped"] == {"ozon.prices": "no keys in the active cabinet"}


def test_rows_sharing_a_key_are_kept_together(db):
    con = snapshots.connect()
    n = snapshots.save(con, "2026-10-15", "wb", "stocks",
                       [{"nmID": 1, "warehouse": "A", "qty": 3}, {"nmID": 1, "warehouse": "B", "qty": 5}])
    assert n == 1
    out = snapshots.read_history("wb", "stocks", "2026-10-15", "2026-10-15", "1", con=con)
    assert [r["warehouse"] for r in out["rows"][0]["record"]] == ["A", "B"]


def test_history_rejects_unknown_kind_and_bad_dates(db):
    mcp = FastMCP("history-test")
    snapshots.register_history_tool(mcp, svc="wb", kinds=["prices"])
    for args in ({"kind": "orders", "date_from": "2026-10-01", "date_to": "2026-10-02"},
                 {"kind": "prices", "date_from": "1.10.2026", "date_to": "2026-10-02"}):
        out = json.loads(asyncio.run(mcp.call_tool("wb_get_history", args))[0][0].text)
        assert out["ok"] is False
