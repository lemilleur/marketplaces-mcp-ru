"""Daily snapshots: per-cabinet history, tolerant sources, legacy layout carried over."""
import asyncio
import hashlib
import json
import sqlite3

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
    monkeypatch.setenv("SNAPSHOT_DB_PATH", str(tmp_path / "s.sqlite"))
    return tmp_path / "s.sqlite"


def use_cabinet(monkeypatch, module, **creds):
    monkeypatch.setattr(module.client.config, "resolve_creds", lambda: (dict(creds), "cab"))
    monkeypatch.setattr(module.client.config, "missing_creds",
                        lambda: [f for f in module.client.config.fields if not creds.get(f)])


def history(svc, module, **args):
    mcp = FastMCP("history-test")
    snapshots.register_history_tool(mcp, svc=svc, kinds=["prices", "stocks"], client=module.client)
    return json.loads(asyncio.run(mcp.call_tool(f"{svc}_get_history", args))[0][0].text)


def test_snapshot_then_history_by_field(db, monkeypatch):
    async def fake_fetch_all(client, spec, **kw):
        assert spec.operation_id == "ozon_prices_get"
        return {"ok": True, "items": [KLIMT], "total_fetched": 1}
    monkeypatch.setattr(core.paginate, "fetch_all", fake_fetch_all)
    use_cabinet(monkeypatch, ozon, client_id="shop-one", api_key="k")
    src = [Source("ozon", "prices", "ozon_mcp.server", "ozon_prices_get")]
    report = asyncio.run(snapshots.take_snapshot("2026-10-15", sources=src))
    assert report["ok"] and report["saved"] == {"ozon.prices": 1}

    out = history("ozon", ozon, kind="prices", date_from="2026-10-01", date_to="2026-10-31",
                  key="ArtKlimtKiss", fields="price.marketing_seller_price,price.min_price")
    assert out["rows"] == [{"date": "2026-10-15", "key": "ArtKlimtKiss",
                            "price.marketing_seller_price": 4200, "price.min_price": 4200}]
    assert out["days_available"] == {"first": "2026-10-15", "last": "2026-10-15", "count": 1}


def test_history_is_isolated_by_cabinet(db, monkeypatch):
    async def fake_fetch_all(client, spec, **kw):
        return {"ok": True, "items": [KLIMT]}
    monkeypatch.setattr(core.paginate, "fetch_all", fake_fetch_all)
    src = [Source("ozon", "prices", "ozon_mcp.server", "ozon_prices_get")]
    use_cabinet(monkeypatch, ozon, client_id="shop-one", api_key="k")
    asyncio.run(snapshots.take_snapshot("2026-10-15", sources=src))
    use_cabinet(monkeypatch, ozon, client_id="shop-two", api_key="k")
    out = history("ozon", ozon, kind="prices", date_from="2026-10-01", date_to="2026-10-31")
    assert out["rows"] == [] and out["days_available"] is None


def test_history_written_by_the_old_addon_is_still_read(db, monkeypatch):
    """Same table and scope format as core.analytics_addon: no history is lost."""
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE snapshots (snap_date TEXT NOT NULL, marketplace TEXT NOT NULL,
        kind TEXT NOT NULL, item_key TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY (snap_date, marketplace, kind, item_key))""")
    scope = "ozon:" + hashlib.sha256(b"shop-one").hexdigest()
    con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)",
                ("2026-10-21", scope, "prices", "ArtKlimtKiss", json.dumps(KLIMT)))
    con.commit()
    con.close()
    use_cabinet(monkeypatch, ozon, client_id="shop-one", api_key="k")
    out = history("ozon", ozon, kind="prices", date_from="2026-10-21", date_to="2026-10-21",
                  fields="price.min_price")
    assert out["rows"] == [{"date": "2026-10-21", "key": "ArtKlimtKiss", "price.min_price": 4200}]


def test_failing_or_keyless_source_does_not_stop_the_rest(db, monkeypatch):
    async def fake_fetch_all(client, spec, **kw):
        if spec.operation_id == "wb_prices_list":
            raise RuntimeError("boom")
        return {"ok": True, "items": [KLIMT]}
    monkeypatch.setattr(core.paginate, "fetch_all", fake_fetch_all)
    use_cabinet(monkeypatch, ozon, client_id="shop-one", api_key="k")
    use_cabinet(monkeypatch, wb, token="t")
    src = [Source("wb", "prices", "wb_mcp.server", "wb_prices_list"),
           Source("ozon", "prices", "ozon_mcp.server", "ozon_prices_get")]
    report = asyncio.run(snapshots.take_snapshot("2026-10-15", sources=src))
    assert "wb.prices" in report["errors"] and report["saved"] == {"ozon.prices": 1}

    use_cabinet(monkeypatch, ozon, client_id="shop-one", api_key="")
    report = asyncio.run(snapshots.take_snapshot("2026-10-16", sources=src[1:]))
    assert report["skipped"] == {"ozon.prices": "no keys in the active cabinet"}


def test_rows_sharing_a_key_are_kept_together(db):
    con = snapshots.connect()
    n = snapshots.save(con, "2026-10-15", "wb:x", "stocks",
                       [{"nmID": 1, "warehouse": "A", "qty": 3}, {"nmID": 1, "warehouse": "B", "qty": 5}])
    assert n == 1
    out = snapshots.read_history("wb:x", "stocks", "2026-10-15", "2026-10-15", "1", con=con)
    assert [r["warehouse"] for r in out["rows"][0]["record"]] == ["A", "B"]


def test_history_rejects_unknown_kind_bad_dates_and_missing_keys(db, monkeypatch):
    use_cabinet(monkeypatch, wb, token="t")
    for args in ({"kind": "orders", "date_from": "2026-10-01", "date_to": "2026-10-02"},
                 {"kind": "prices", "date_from": "1.10.2026", "date_to": "2026-10-02"}):
        assert history("wb", wb, **args)["ok"] is False
    use_cabinet(monkeypatch, wb, token="")
    out = history("wb", wb, kind="prices", date_from="2026-10-01", date_to="2026-10-02")
    assert out["ok"] is False and out["error"] == "auth"
