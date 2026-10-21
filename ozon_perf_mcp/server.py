#!/usr/bin/env python3
"""ozon_perf_mcp — MCP server for the Ozon Performance (advertising) API.

Exposes the whole Ozon Performance API through the same schema-driven meta-tools
(search / describe / call / fetch_all) used by the other servers. Single host
(api-performance.ozon.ru). Credentials are SEPARATE from the Ozon Seller API.

Auth: OAuth2 client_credentials.
    POST https://api-performance.ozon.ru/api/client/token
         {"client_id": ..., "client_secret": ..., "grant_type": "client_credentials"}
      -> 200 {"access_token": "...", "expires_in": 1800, "token_type": "Bearer"}
    Every subsequent request sends: Authorization: Bearer <access_token>

The token fetch/cache/refresh lives entirely in core.client.MarketplaceClient and
is enabled purely by setting `token_url` on the ServiceConfig below.

!!! UNVERIFIED CONTRACT !!!
The token contract above is taken from Ozon's documentation but has NOT been
exercised against the live API (no Performance credentials were available when
this server was written). If live calls fail at the token step, the single place
to adjust is MarketplaceClient._fetch_token in core/client.py (field names, JSON
vs form-encoding, token_type handling).

Run:
    OZON_PERF_CLIENT_ID=... OZON_PERF_CLIENT_SECRET=... python -m ozon_perf_mcp.server
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from core.client import MarketplaceClient, ServiceConfig
from core.entities import EntityIndex
from core.registry import Catalog
from core.snapshots import register_history_tool
from core.tools import register_cabinet_tools, register_generic_tools
from core.workflows import Workflows, register_workflow_tools
from core.transport import run as run_transport

# The perf catalog lives next to the Ozon Seller catalog (already prepared:
# 45 endpoints, host api-performance.ozon.ru, default_host set).
CATALOG_PATH = Path(__file__).resolve().parent.parent / "ozon_mcp" / "perf_endpoints.yaml"
WORKFLOWS_PATH = Path(__file__).with_name("workflows.yaml")


def _build_headers(creds: dict[str, str]) -> dict[str, str]:
    """Non-auth default headers. Auth is handled by the OAuth bearer flow in
    MarketplaceClient (token_url set below), so this only sets Content-Type."""
    return {"Content-Type": "application/json"}


OZON_PERF_CONFIG = ServiceConfig(
    name="ozon_perf",
    scheme="https",
    fields=["client_id", "client_secret"],
    env_map={
        "client_id": "OZON_PERF_CLIENT_ID",
        "client_secret": "OZON_PERF_CLIENT_SECRET",
    },
    build_headers=_build_headers,
    # Enabling OAuth2 client_credentials. UNVERIFIED live (see module docstring).
    token_url="https://api-performance.ozon.ru/api/client/token",
    oauth_id_field="client_id",
    oauth_secret_field="client_secret",
    # OAuth client_secret + bearer may only be sent to Ozon hosts.
    allowed_host_suffixes=[".ozon.ru"],
    api_docs="https://docs.ozon.ru/api/performance/",
)

mcp = FastMCP("ozon_perf_mcp")
entities = EntityIndex.load()
catalog = Catalog.from_yaml(CATALOG_PATH, entities=entities)
client = MarketplaceClient(OZON_PERF_CONFIG)

register_generic_tools(
    mcp, svc="ozon_perf", client=client, catalog=catalog, entities=entities,
    key_help=("seller.ozon.ru → Performance API → API keys "
              "(Client-Id + Client-Secret; SEPARATE from Seller API keys)."),
)
register_cabinet_tools(mcp, svc="ozon_perf", client=client, catalog=catalog)
register_workflow_tools(
    mcp, svc="ozon_perf", workflows=Workflows.from_yaml(WORKFLOWS_PATH))
register_history_tool(mcp, svc="ozon_perf", kinds=["campaigns"])


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)



# --------------------------------------------------------------------------
# Daily statistics as JSON.
#
# GET /api/client/statistics/daily answers with semicolon CSV, decimal comma,
# one row per campaign per day. Live header on 2026-10-21:
#   ID;Название;Дата;Показы;Клики;Расход, ₽;Заказы, шт.;Заказы, ₽
# Through ozon_perf_call_method the model receives that CSV as one string and
# has to parse "916,71" itself. This tool returns numbers.
# --------------------------------------------------------------------------
_COLS = {"ID": "campaign_id", "Название": "campaign", "Дата": "date",
         "Показы": "views", "Клики": "clicks", "Расход, ₽": "spend_rub",
         "Заказы, шт.": "orders", "Заказы, ₽": "orders_rub"}


def _number(text: str):
    t = (text or "").strip().replace("\u00a0", "").replace(" ", "")
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+[.,]\d+", t):
        return float(t.replace(",", "."))
    return (text or "").strip()


def parse_daily_csv(text: str) -> list[dict]:
    """Semicolon CSV from statistics/daily -> rows with English keys and numbers."""
    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        return []
    rows = []
    for raw in csv.DictReader(io.StringIO(text), delimiter=";"):
        row = {}
        for key, value in raw.items():
            if key is None:
                continue
            name = _COLS.get(key.strip(), key.strip())
            row[name] = (value or "").strip() if name in ("campaign_id", "campaign", "date") \
                else _number(value)
        rows.append(row)
    return rows


def summarize_by_day(rows: list[dict]) -> list[dict]:
    """Totals across campaigns per day, with ad cost share (ДРР) of ad-attributed orders."""
    days: dict[str, dict] = {}
    for r in rows:
        d = days.setdefault(r.get("date", ""), {"date": r.get("date", ""), "views": 0,
                            "clicks": 0, "spend_rub": 0.0, "orders": 0, "orders_rub": 0.0})
        for k in ("views", "clicks", "spend_rub", "orders", "orders_rub"):
            v = r.get(k)
            d[k] += v if isinstance(v, (int, float)) else 0
    out = []
    for d in sorted(days.values(), key=lambda x: x["date"]):
        d["spend_rub"] = round(d["spend_rub"], 2)
        d["orders_rub"] = round(d["orders_rub"], 2)
        d["drr_pct"] = round(d["spend_rub"] / d["orders_rub"] * 100, 1) if d["orders_rub"] else None
        out.append(d)
    return out


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def ozon_perf_get_daily_stats(date_from: str, date_to: str) -> str:
    """Ad statistics per campaign per day as JSON numbers, plus totals per day with ДРР.

    date_from, date_to: YYYY-MM-DD. Wraps GET /api/client/statistics/daily.
    Ozon attributes an ad order to the CLICK date, so per-day ad orders do not
    match per-day seller orders; compare over a week, not a day.
    """
    for d in (date_from, date_to):
        try:
            if date.fromisoformat(d).isoformat() != d:
                raise ValueError
        except ValueError:
            return _j({"ok": False, "error": "dates_must_be_YYYY_MM_DD"})
    res = await client.call_spec(catalog.get("ozonperf_get_api_client_statistics_daily"),
                                 query={"dateFrom": date_from, "dateTo": date_to})
    if not res.get("ok"):
        return _j(res)
    data = res.get("data")
    if not isinstance(data, str):
        return _j(res)  # Ozon switched to JSON: pass it through untouched
    rows = parse_daily_csv(data)
    return _j({"ok": True, "rows": rows, "by_day": summarize_by_day(rows),
               "note": "Ad orders are attributed to the click date."})

def main() -> None:
    """Console entry point: stdio by default, HTTP with MCP_TRANSPORT=http."""
    run_transport(mcp)


if __name__ == "__main__":
    main()
