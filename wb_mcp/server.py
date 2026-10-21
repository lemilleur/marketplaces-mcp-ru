#!/usr/bin/env python3
"""wb_mcp — MCP server for the Wildberries Seller API.

Exposes the whole WB Seller API through schema-driven meta-tools (search /
describe / call / fetch_all) plus a few typed convenience tools for the most
common manager tasks. Multi-host aware. Credentials come from the environment.

Auth: WB uses ONE token, sent in the `Authorization` header as the raw value
(no "Bearer " prefix). The token is scoped per category — a token must include
the category of the host it calls.

Run:
    WB_API_TOKEN=... python -m wb_mcp.server
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP

from core.client import MarketplaceClient, ServiceConfig
from core.entities import EntityIndex
from core.registry import Catalog
from core.tools import register_cabinet_tools, register_generic_tools
from core.workflows import Workflows, register_workflow_tools
from core.transport import run as run_transport

CATALOG_PATH = Path(__file__).with_name("endpoints.yaml")
WORKFLOWS_PATH = Path(__file__).with_name("workflows.yaml")


def _build_headers(creds: dict[str, str]) -> dict[str, str]:
    # Raw token, no "Bearer" prefix (per WB docs / community practice).
    return {"Authorization": creds.get("token", "")}


WB_CONFIG = ServiceConfig(
    name="wb",
    scheme="https",
    fields=["token"],
    env_map={"token": "WB_API_TOKEN"},
    build_headers=_build_headers,
    whoami=("wb_get_api_seller_info", ["name", "tradeMark"]),
    # WB is multi-host, but every host lives under wildberries.ru. Auth headers
    # (the raw seller token) may only ever be sent there.
    allowed_host_suffixes=[".wildberries.ru"],
    api_docs="https://dev.wildberries.ru/en/openapi/api-information",
)

mcp = FastMCP("wb_mcp")
entities = EntityIndex.load()
catalog = Catalog.from_yaml(CATALOG_PATH, entities=entities)
client = MarketplaceClient(WB_CONFIG)

# Register the generic schema-driven tools (wb_search_methods, wb_call_method,
# wb_write_method, wb_delete_method, wb_get_raw, ...)
register_generic_tools(
    mcp, svc="wb", client=client, catalog=catalog, entities=entities,
    key_help="seller.wildberries.ru → Settings → Access tokens (one token, "
             "select the categories you need).",
)
register_cabinet_tools(mcp, svc="wb", client=client, catalog=catalog)
register_workflow_tools(mcp, svc="wb", workflows=Workflows.from_yaml(WORKFLOWS_PATH))


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# --------------------------------------------------------------------------
# Typed convenience tools — the everyday manager workflows, one call each.
# They delegate to the same client; nothing is duplicated.
# --------------------------------------------------------------------------
@mcp.tool(
    name="wb_get_sales",
    annotations={"title": "WB sales & returns", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_sales(date_from: str, flag: int = 0) -> str:
    """LEGACY Statistics sales and returns (may be incomplete or empty).

    Prefer wb_get_order_feed for current operations and wb_get_finance_report
    for financial reconciliation and spp. Empty legacy data is not zero sales.

    Args:
        date_from: RFC3339 date/time in MSK, e.g. "2026-06-01" or "2026-06-01T00:00:00".
        flag: 0 = rows changed since date_from (incremental); 1 = rows dated on date_from.
    Returns JSON: {"ok": true, "status", "data": [ sale rows ]} or error envelope.
    Each row includes saleID, srid, nmId, totalPrice, forPay, lastChangeDate.
    """
    spec = catalog.get("wb_stats_sales")
    result = await client.call_spec(spec, query={"dateFrom": date_from, "flag": flag})
    if result.get("ok") and not result.get("data"):
        result["warning"] = ("Legacy Statistics returned no rows. This does not prove zero sales. "
                             "Use wb_get_order_feed for operational orders and "
                             "wb_get_finance_report for reconciliation and spp.")
    return _j(result)



# --------------------------------------------------------------------------
# Sales funnel by day for EVERY card in one call.
#
# /api/analytics/v3/sales-funnel/products/history rejects an empty nmIds
# ("at least 1 nmId is expected", checked live 2026-10-21), so through
# wb_call_method the model first has to page the card list and then batch.
# --------------------------------------------------------------------------
FUNNEL_BATCH = 20            # nmIds per history request
FUNNEL_PAUSE_SEC = 20.0      # analytics host allows about 3 requests per minute


async def _all_nm_ids() -> tuple[list[int], dict | None]:
    spec = catalog.get("wb_content_cards_list")
    cursor: dict = {"limit": 100}
    ids: list[int] = []
    while True:
        res = await client.call_spec(spec, json_body={
            "settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
        if not res.get("ok"):
            return ids, res
        data = res.get("data") or {}
        cards = data.get("cards") or []
        ids.extend(c["nmID"] for c in cards if isinstance(c, dict) and "nmID" in c)
        nxt = data.get("cursor") or {}
        if len(cards) < cursor["limit"] or not nxt.get("nmID"):
            return ids, None
        cursor = {"limit": 100, "updatedAt": nxt.get("updatedAt"), "nmID": nxt.get("nmID")}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def wb_get_funnel_history(date_from: str, date_to: str, nm_ids: str = "") -> str:
    """WB sales funnel by day: card opens, add-to-cart, orders, buyouts, conversions.

    date_from, date_to: YYYY-MM-DD. nm_ids: comma-separated nmID filter; empty =
    every card of the seller (the tool pages the card list itself). Requests go
    in batches of 20 nmIds with a pause between them, because the analytics host
    allows about 3 requests per minute: a large catalogue takes minutes.
    Returns the history rows of every batch, untouched.
    """
    if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in (date_from, date_to)):
        return _j({"ok": False, "error": "dates_must_be_YYYY_MM_DD"})
    if nm_ids.strip():
        try:
            ids = [int(x) for x in nm_ids.split(",") if x.strip()]
        except ValueError:
            return _j({"ok": False, "error": "nm_ids_must_be_comma_separated_integers"})
    else:
        ids, err = await _all_nm_ids()
        if err:
            return _j(err)
    if not ids:
        return _j({"ok": False, "error": "no_cards_found"})
    spec = catalog.get("wb_post_api_analytics_sales_funnel_products_history")
    rows: list = []
    for i in range(0, len(ids), FUNNEL_BATCH):
        if i:
            await asyncio.sleep(FUNNEL_PAUSE_SEC)
        res = await client.call_spec(spec, json_body={
            "selectedPeriod": {"start": date_from, "end": date_to},
            "nmIds": ids[i:i + FUNNEL_BATCH], "skipDeletedNm": True,
            "aggregationLevel": "day"})
        if not res.get("ok"):
            res["fetched_before_error"] = len(rows)
            return _j(res)
        data = res.get("data")
        batch = data.get("data", data) if isinstance(data, dict) else data
        rows.extend(batch if isinstance(batch, list) else [batch])
    return _j({"ok": True, "nm_count": len(ids), "rows": rows})

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def wb_get_order_feed(date_from: str, date_to: str, limit: int = 1000,
                            offset: int = 0, snapshot_time: Optional[str] = None) -> str:
    """Current WB orders, buyouts and returns: replacement for legacy Statistics.

    Dates must be full RFC3339 with timezone, e.g. 2026-10-01T00:00:00+03:00.
    Analytics permission required. Maximum period: last 31 days. Period filters
    CURRENT STATUS time, not necessarily order creation time. One row is one item.
    Page using pagination offset and the same returned data.snapshotTime.
    Do not treat current statuses as a complete historical event log.
    For financial amounts and spp use wb_get_finance_report instead.
    """
    if not 1 <= limit <= 1000 or offset < 0:
        return _j({"ok": False, "error": "invalid_limit_or_offset"})
    try:
        start = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
        if not start.tzinfo or not end.tzinfo or not 0 < (end - start).total_seconds() <= 31 * 86400:
            raise ValueError("invalid period")
    except ValueError:
        return _j({"ok": False, "error": "use_RFC3339_with_timezone_and_period_up_to_31_days"})
    paging = {"limit": limit, "offset": offset}
    if snapshot_time:
        paging["snapshotTime"] = snapshot_time
    return _j(await client.call_spec(catalog.get("wb_order_feed"), json_body={
        "selectedPeriod": {"start": date_from, "end": date_to},
        "pagination": paging, "nmIds": [], "subjectIds": [], "brandNames": [], "tagIds": []}))


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def wb_get_finance_report(date_from: str, date_to: str, limit: int = 1000,
                                rrd_id: int = 0, period: str = "weekly") -> str:
    """WB realization details, replacing retired reportDetailByPeriod v5.

    Finance permission required. Money fields are decimal STRINGS (use Decimal).
    CamelCase fields include rrdId, retailAmount, forPay, sellerOperName and spp.
    spp is supplied by WB; storage/logistics rows are not sales.
    Page with last row rrdId until HTTP 204, even after a short page.
    Weekly/daily reports reflect report periods, not a real-time sales feed.
    """
    if not 1 <= limit <= 100000 or rrd_id < 0 or period not in ("weekly", "daily"):
        return _j({"ok": False, "error": "invalid_report_parameters"})
    return _j(await client.call_spec(catalog.get("wb_report_realization"), json_body={
        "dateFrom": date_from, "dateTo": date_to, "limit": limit,
        "rrdId": rrd_id, "period": period}))


@mcp.tool(
    name="wb_get_stocks",
    annotations={"title": "WB current stocks", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_stocks(limit: int = 1000, offset: int = 0) -> str:
    """Get current WB warehouse stocks using the Analytics API.

    Requires the Analytics token category. Data refreshes every 30 minutes.
    Use offset pagination; the deprecated Statistics stocks method is not used.
    Response follows the current Analytics schema, not the old flat stock list.
    """
    if not 1 <= limit <= 250000 or offset < 0:
        return _j({"ok": False, "error": "invalid_limit_or_offset"})
    spec = catalog.get("wb_post_api_analytics_stocks_report_wb_warehouses")
    return _j(await client.call_spec(
        spec, json_body={"nmIds": [], "chrtIds": [], "limit": limit, "offset": offset}))


@mcp.tool(
    name="wb_get_new_orders",
    annotations={"title": "WB new FBS orders", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_new_orders() -> str:
    """Get new FBS assembly orders awaiting processing (Marketplace API).

    Returns JSON: {"ok": true, "data": {"orders": [...]}} — each order has id,
    rid, article, skus, createdAt, warehouseId.
    """
    spec = catalog.get("wb_fbs_orders_new")
    return _j(await client.call_spec(spec))


@mcp.tool(
    name="wb_get_prices",
    annotations={"title": "WB prices & discounts", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_prices(limit: int = 1000, offset: int = 0,
                        filter_nm_id: Optional[int] = None) -> str:
    """Get current prices and discounts for products (Discounts-Prices API).

    Args:
        limit: page size (<=1000).
        offset: pagination offset.
        filter_nm_id: optional single nmID to filter by.
    Returns JSON: {"ok": true, "data": {"listGoods": [{nmID, sizes, discount, ...}]}}.
    """
    q = {"limit": min(limit, 1000), "offset": offset}
    if filter_nm_id is not None:
        q["filterNmID"] = filter_nm_id
    spec = catalog.get("wb_prices_list")
    return _j(await client.call_spec(spec, query=q))


@mcp.tool(
    name="wb_set_price",
    annotations={"title": "WB set price/discount", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def wb_set_price(nm_id: int, price: int, discount: int = 0,
                       confirm_write: bool = False) -> str:
    """Set price and discount for ONE product (Discounts-Prices API). WRITE.

    Requires confirm_write=true (this changes your live price). A new price 3x
    below the old one lands the product in WB price quarantine.

    Args:
        nm_id: product nmID.
        price: new base price in rubles (integer).
        discount: discount percent (0-99).
        confirm_write: must be true to actually send the change.
    Returns JSON: {"ok": true, "data": {"id": uploadID}} — poll wb_prices_history_tasks.
    """
    from core.safety import check_gate
    gate = check_gate("write", confirm_write=confirm_write,
                      i_understand_this_modifies_data=True,
                      operation_id="wb_set_price", endpoint="/api/v2/upload/task")
    if gate:
        return _j(gate)
    spec = catalog.get("wb_prices_set")
    body = {"data": [{"nmID": nm_id, "price": price, "discount": discount}]}
    return _j(await client.call_spec(spec, json_body=body))


def main() -> None:
    """Console entry point: stdio by default, HTTP with MCP_TRANSPORT=http."""
    run_transport(mcp)


if __name__ == "__main__":
    main()
