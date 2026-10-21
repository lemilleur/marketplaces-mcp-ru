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
    monkeypatch.setenv("TELEGRAM_CHATS", "team:123")
    assert not call(text="report", to="456", confirm_send=True)["ok"]
    assert not call(text=" ", confirm_send=True)["ok"]


def test_split_and_delivery(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-value")
    monkeypatch.setenv("TELEGRAM_CHATS", "team:123")
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
    monkeypatch.setenv("TELEGRAM_CHATS", "team:123")
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


def test_proxy_only_configures_telegram_client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-value")
    monkeypatch.setenv("TELEGRAM_CHATS", "team:123")
    proxy = "http://user:password@proxy.example:8080"
    monkeypatch.setenv("TELEGRAM_PROXY_URL", proxy)
    real_client = httpx.AsyncClient
    options = {}
    def factory(**kw):
        options.update(kw)
        kw.pop("proxy")
        return real_client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})), **kw)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert call(text="report", confirm_send=True)["ok"]
    assert options["proxy"] == proxy
    assert options["trust_env"] is False


def test_invalid_proxy_never_sends(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-value")
    monkeypatch.setenv("TELEGRAM_CHATS", "team:123")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "socks5://user:secret@proxy.example:1080")
    result = call(text="report", confirm_send=True)
    assert result["error"] == "invalid_telegram_proxy_url"
    assert "secret" not in json.dumps(result)
