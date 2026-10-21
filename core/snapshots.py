"""Daily snapshots: the history the marketplace APIs do not keep.

Ozon and WB answer "what is the price / stock / campaign state NOW". The
question behind a sales spike is usually "what was it on the 15th", and no
endpoint answers that: auto-promotions move the buyer price inside the
min_price corridor on their own, campaigns stop on budget, stock leaves a
cluster. Without Ozon Premium analytics only reach back three months anyway.

Run once a day (cron in the container, e.g. 06:00 Moscow time):

    python -m core.snapshots            # snapshot every service that has keys
    python -m core.snapshots --date 2026-10-21

Each service's catalog and client are reused, so the active cabinet's keys
and every request guard apply unchanged. Only read endpoints are listed.

Storage: SQLite at $SNAPSHOT_DB_PATH, default ~/.marketplace-mcp/snapshots.sqlite
next to cabinets.json. In Docker that directory must sit on a persistent volume
or the history dies with the container. The table layout is the one the
earlier core.analytics_addon wrote, so its history carries over.

History is scoped per cabinet: the `marketplace` column holds
"<service>:<sha256 of the cabinet's first key field>", so two shops (or two
cabinets with the same name) never mix, and nothing secret is stored. Rows are
keyed (date, scope, kind, item key); re-running a day overwrites that day.

The `{svc}_get_history` tools read the store; they never call a marketplace.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .credentials import STORE_DIR

MSK = timezone(timedelta(hours=3))
KEY_FIELDS = ("offer_id", "nmID", "nmId", "nm_id", "id", "sku")


def db_path() -> Path:
    return Path(os.environ.get("SNAPSHOT_DB_PATH", STORE_DIR / "snapshots.sqlite"))


def scope_of(service: str, client: Any) -> str:
    """'<service>:<sha256 of the active cabinet's first key field>'.

    Raises ValueError when that field is empty: history without an identity
    could silently merge two shops.
    """
    creds, _ = client.config.resolve_creds()
    identity = creds.get(client.config.fields[0], "") if client.config.fields else ""
    if not identity:
        raise ValueError(f"no keys for {service} in the active cabinet")
    return f"{service}:{hashlib.sha256(identity.encode()).hexdigest()}"


@dataclass
class Source:
    service: str              # history namespace: ozon | ozon_perf | wb
    kind: str                 # prices | stocks | campaigns
    module: str               # server module that owns client + catalog
    operation_id: str
    body: Optional[dict] = None
    query: Optional[dict] = None
    items_path: Optional[str] = None
    notes: list[str] = field(default_factory=list)


SOURCES: list[Source] = [
    Source("ozon", "prices", "ozon_mcp.server", "ozon_prices_get",
           body={"filter": {"visibility": "ALL"}, "limit": 1000}),
    Source("ozon", "stocks", "ozon_mcp.server", "ozon_stocks_info",
           body={"filter": {"visibility": "ALL"}, "limit": 1000}),
    # Campaign list answers {"list": [...]}; the catalog default result.items is
    # not its shape.
    Source("ozon_perf", "campaigns", "ozon_perf_mcp.server", "ozonperf_get_api_client_campaign",
           items_path="list"),
    Source("wb", "prices", "wb_mcp.server", "wb_prices_list", query={"limit": 1000}),
    Source("wb", "stocks", "wb_mcp.server", "wb_post_api_analytics_stocks_report_wb_warehouses",
           body={"nmIds": [], "chrtIds": [], "limit": 1000}),
]


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        snap_date TEXT NOT NULL, marketplace TEXT NOT NULL, kind TEXT NOT NULL,
        item_key TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY (snap_date, marketplace, kind, item_key))""")
    return con


def item_key(item: Any) -> str:
    if isinstance(item, dict):
        for f in KEY_FIELDS:
            if item.get(f) not in (None, ""):
                return str(item[f])
    return "_all"


def save(con: sqlite3.Connection, snap_date: str, scope: str, kind: str,
         items: list[Any]) -> int:
    """Store items grouped by key; several rows per key (stock per warehouse) stay a list."""
    grouped: dict[str, list[Any]] = {}
    for it in items:
        grouped.setdefault(item_key(it), []).append(it)
    with con:
        con.executemany(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
            [(snap_date, scope, kind, key,
              json.dumps(rows[0] if len(rows) == 1 else rows, ensure_ascii=False))
             for key, rows in grouped.items()])
    return len(grouped)


async def take_snapshot(snap_date: Optional[str] = None,
                        sources: Optional[list[Source]] = None,
                        con: Optional[sqlite3.Connection] = None) -> dict:
    """Snapshot every source whose service has keys. One failing source does not stop the rest."""
    from .paginate import fetch_all

    day = snap_date or datetime.now(MSK).date().isoformat()
    own = con is None
    con = con or connect()
    report: dict[str, Any] = {"date": day, "saved": {}, "skipped": {}, "errors": {}}
    try:
        for src in sources or SOURCES:
            name = f"{src.service}.{src.kind}"
            try:
                mod = importlib.import_module(src.module)
                if mod.client.config.missing_creds():
                    report["skipped"][name] = "no keys in the active cabinet"
                    continue
                res = await fetch_all(mod.client, mod.catalog.get(src.operation_id),
                                      base_body=src.body, base_query=src.query,
                                      items_path=src.items_path)
                if not res.get("ok"):
                    report["errors"][name] = res.get("message") or res.get("error")
                    continue
                report["saved"][name] = save(con, day, scope_of(src.service, mod.client),
                                             src.kind, res["items"])
                if res.get("truncated"):
                    report["errors"][name] = "truncated by fetch_all limits"
            except Exception as exc:  # noqa: BLE001 — record and move on
                report["errors"][name] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        if own:
            con.close()
    report["ok"] = not report["errors"]
    return report


def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def read_history(scope: str, kind: str, date_from: str, date_to: str,
                 key: str = "", fields: Optional[list[str]] = None,
                 con: Optional[sqlite3.Connection] = None, limit: int = 5000) -> dict:
    own = con is None
    con = con or connect()
    try:
        q = ("SELECT snap_date, item_key, payload FROM snapshots WHERE marketplace=? AND kind=? "
             "AND snap_date BETWEEN ? AND ?")
        args: list[Any] = [scope, kind, date_from, date_to]
        if key:
            q += " AND item_key=?"
            args.append(key)
        rows = con.execute(q + " ORDER BY snap_date, item_key LIMIT ?", args + [limit + 1]).fetchall()
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT snap_date FROM snapshots WHERE marketplace=? AND kind=? "
            "ORDER BY snap_date", [scope, kind]).fetchall()]
    finally:
        if own:
            con.close()
    out = []
    for snap_date, item, payload in rows[:limit]:
        rec = json.loads(payload)
        out.append({"date": snap_date, "key": item,
                    **({f: _dig(rec, f) for f in fields} if fields else {"record": rec})})
    return {"ok": True, "rows": out, "truncated": len(rows) > limit,
            "days_available": {"first": days[0], "last": days[-1], "count": len(days)} if days
            else None}


def register_history_tool(mcp: Any, *, svc: str, kinds: list[str], client: Any) -> None:
    """`{svc}_get_history`: read the daily snapshot store. Never calls the marketplace."""
    kinds_text = " | ".join(kinds)

    async def get_history(kind: str, date_from: str, date_to: str,
                          key: str = "", fields: str = "") -> str:
        if kind not in kinds:
            return json.dumps({"ok": False, "error": "unknown_kind", "kinds": kinds})
        try:
            for d in (date_from, date_to):
                if date.fromisoformat(d).isoformat() != d:
                    raise ValueError
        except ValueError:
            return json.dumps({"ok": False, "error": "dates_must_be_YYYY_MM_DD"})
        field_list = [f.strip() for f in fields.split(",") if f.strip()] or None
        try:
            scope = scope_of(svc, client)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": "auth", "message": str(exc)})
        return json.dumps(read_history(scope, kind, date_from, date_to, key, field_list),
                          ensure_ascii=False, indent=2, default=str)

    get_history.__doc__ = f"""Daily history from local snapshots (taken once a day by cron).

    kind: {kinds_text}. date_from, date_to: YYYY-MM-DD.
    key: one item — offer_id for Ozon, nmID for WB, campaign id for ads; empty = all.
    fields: comma-separated dotted paths to keep, e.g.
    "price.marketing_seller_price,price.min_price,marketing_actions.actions"; empty = whole record.
    Answers "what was the price / stock / campaign state on that day" for the
    ACTIVE cabinet only. Days before the first snapshot do not exist: see
    days_available in the answer.
    """
    mcp.tool(name=f"{svc}_get_history",
             annotations={"readOnlyHint": True, "openWorldHint": False})(get_history)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Take a daily snapshot of prices, stocks, campaigns.")
    ap.add_argument("--date", help="snapshot date YYYY-MM-DD (default: today, Moscow time)")
    args = ap.parse_args(argv)
    report = asyncio.run(take_snapshot(args.date))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
