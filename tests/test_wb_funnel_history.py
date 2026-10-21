"""WB funnel history: collects every nmID itself and batches the history calls."""
import asyncio
import json

from wb_mcp import server as wb


class Calls:
    def __init__(self, cards_pages):
        self.cards_pages = iter(cards_pages)
        self.history_bodies = []

    async def call_spec(self, spec, **kwargs):
        if spec.operation_id == "wb_content_cards_list":
            return {"ok": True, "data": next(self.cards_pages)}
        self.history_bodies.append(kwargs["json_body"])
        return {"ok": True, "data": {"data": [{"nmId": n} for n in kwargs["json_body"]["nmIds"]]}}


def test_funnel_pages_cards_and_batches_history(monkeypatch):
    first = {"cards": [{"nmID": n} for n in range(100)], "cursor": {"updatedAt": "t", "nmID": 99}}
    second = {"cards": [{"nmID": n} for n in range(100, 125)], "cursor": {"updatedAt": "t2", "nmID": 124}}
    calls = Calls([first, second])
    monkeypatch.setattr(wb.client, "call_spec", calls.call_spec)
    monkeypatch.setattr(wb, "FUNNEL_PAUSE_SEC", 0)
    out = json.loads(asyncio.run(wb.wb_get_funnel_history("2026-10-14", "2026-10-20")))
    assert out["ok"] and out["nm_count"] == 125 and len(out["rows"]) == 125
    assert [len(b["nmIds"]) for b in calls.history_bodies] == [20, 20, 20, 20, 20, 20, 5]
    assert calls.history_bodies[0]["selectedPeriod"] == {"start": "2026-10-14", "end": "2026-10-20"}
    assert calls.history_bodies[0]["aggregationLevel"] == "day"


def test_funnel_uses_explicit_nm_ids_without_card_list(monkeypatch):
    calls = Calls([])
    monkeypatch.setattr(wb.client, "call_spec", calls.call_spec)
    out = json.loads(asyncio.run(wb.wb_get_funnel_history("2026-10-14", "2026-10-20", "11, 22")))
    assert out["ok"] and calls.history_bodies[0]["nmIds"] == [11, 22]


def test_funnel_rejects_bad_dates():
    out = json.loads(asyncio.run(wb.wb_get_funnel_history("14.10.2026", "2026-10-20")))
    assert out["ok"] is False
