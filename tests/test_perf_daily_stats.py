"""Ozon Performance daily statistics: CSV with decimal commas becomes JSON numbers."""
import asyncio
import json
from unittest.mock import AsyncMock

from ozon_perf_mcp import server as perf

# Real response shape from 2026-10-21 (two campaigns, one day).
LIVE_CSV = ("ID;Название;Дата;Показы;Клики;Расход, ₽;Заказы, шт.;Заказы, ₽\n"
            "45308544;делюксы;2026-10-20;4583;190;1435,27;3;12600,00\n"
            "45308553;котики;2026-10-20;386;15;293,13;0;0,00\n")


def test_daily_csv_rows_are_numbers():
    rows = perf.parse_daily_csv(LIVE_CSV)
    assert rows[0] == {"campaign_id": "45308544", "campaign": "делюксы", "date": "2026-10-20",
                       "views": 4583, "clicks": 190, "spend_rub": 1435.27,
                       "orders": 3, "orders_rub": 12600.0}


def test_daily_tool_sums_days_and_computes_drr(monkeypatch):
    call = AsyncMock(return_value={"ok": True, "status": 200, "data": LIVE_CSV})
    monkeypatch.setattr(perf.client, "call_spec", call)
    out = json.loads(asyncio.run(perf.ozon_perf_get_daily_stats("2026-10-20", "2026-10-20")))
    assert out["by_day"] == [{"date": "2026-10-20", "views": 4969, "clicks": 205,
                              "spend_rub": 1728.4, "orders": 3, "orders_rub": 12600.0,
                              "drr_pct": 13.7}]
    assert call.call_args.kwargs["query"] == {"dateFrom": "2026-10-20", "dateTo": "2026-10-20"}


def test_daily_tool_rejects_bad_dates(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(perf.client, "call_spec", call)
    out = json.loads(asyncio.run(perf.ozon_perf_get_daily_stats("20.10.2026", "2026-10-20")))
    assert out["ok"] is False
    call.assert_not_called()
