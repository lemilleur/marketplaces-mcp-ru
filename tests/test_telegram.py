import asyncio
import json

import httpx

from core import telegram


def call(**kwargs):
    return json.loads(asyncio.run(telegram.telegram_send(**kwargs)))


def test_confirmation_and_allowlist_prevent_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network must not be used")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert call(text="report")["error"] == "confirmation_required"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-value")
    monkeypatch.setenv("TELEGRAM_CHATS", "me:123")
    assert not call(text="report", to="456", confirm_send=True)["ok"]
    assert not call(text=" ", confirm_send=True)["ok"]


def test_split_and_delivery(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-value")
    monkeypatch.setenv("TELEGRAM_CHATS", "me:123")
    real_client = httpx.AsyncClient
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    report = "😀" * 3000
    assert call(text=report, confirm_send=True)["sent_parts"] == 2
    assert "".join(p["text"] for p in payloads) == report
    assert all(len(p["text"].encode("utf-16-le")) // 2 <= 4000 for p in payloads)
    assert all(p["chat_id"] == "123" for p in payloads)


def test_timeout_does_not_expose_token_or_retry(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-value")
    monkeypatch.setenv("TELEGRAM_CHATS", "me:123")
    real_client = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout(str(request.url))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    result = call(text="report", confirm_send=True)
    assert result["delivery_uncertain"]
    assert len(requests) == 1
    assert "test-value" not in json.dumps(result)
