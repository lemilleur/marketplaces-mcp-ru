"""Optional report delivery to explicitly configured Telegram chats."""
from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx


async def telegram_send(text: str, to: str = "team", confirm_send: bool = False) -> str:
    """Send a plain-text report to a configured chat alias. Requires confirm_send.

    Ask the user to approve the report and recipient before setting confirm_send.
    Recipients come only from TELEGRAM_CHATS; raw chat IDs are not accepted.
    The default recipient is "team", the configured Telegram group. Omit `to`
    to send to that group; never substitute "me" unless explicitly configured.
    Reports are limited to 40,000 characters and split into 4,000-character parts.
    Do not retry a whole report after partial or uncertain delivery: duplicates
    are possible. No HTML parsing or file attachments are supported.
    """
    def result(**values):
        return json.dumps(values, ensure_ascii=False)

    if not confirm_send:
        return result(ok=False, error="confirmation_required", sent_parts=0)
    if not text.strip() or len(text) > 40000:
        return result(ok=False, error="text_must_have_1_to_40000_characters", sent_parts=0)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = {}
    for pair in os.environ.get("TELEGRAM_CHATS", "").split(","):
        alias, sep, chat_id = pair.strip().partition(":")
        alias, chat_id = alias.strip(), chat_id.strip()
        if sep and alias and chat_id.lstrip("-").isdigit():
            if alias in chats:
                return result(ok=False, error="duplicate_chat_alias", sent_parts=0)
            chats[alias] = chat_id
    if not token or to not in chats:
        return result(ok=False, error="telegram_not_configured_or_recipient_not_allowed", sent_parts=0)

    proxy = os.environ.get("TELEGRAM_PROXY_URL", "").strip() or None
    if proxy:
        try:
            parsed = urlsplit(proxy)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.port:
                raise ValueError("invalid proxy")
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("invalid proxy")
        except ValueError:
            return result(ok=False, error="invalid_telegram_proxy_url", sent_parts=0)

    # UTF-16 units stay below Telegram's limit even for emoji-heavy reports.
    parts, current, units = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xffff else 1
        if units + width > 4000:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        parts.append("".join(current))

    sent = 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                 proxy=proxy, trust_env=False) as client:
        for part in parts:
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chats[to], "text": part,
                              "link_preview_options": {"is_disabled": True}},
                    )
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("unexpected response")
                except (httpx.HTTPError, ValueError):
                    # Exception messages may contain the bot token in the URL.
                    return result(ok=False, error="delivery_unknown", sent_parts=sent,
                                  delivery_uncertain=True)
                if response.status_code == 429 and attempt < 2:
                    delay = data.get("parameters", {}).get("retry_after", 3)
                    if isinstance(delay, (int, float)) and 0 <= delay <= 10:
                        await asyncio.sleep(delay)
                        continue
                if not response.is_success or data.get("ok") is not True:
                    return result(ok=False, error="telegram_rejected_request",
                                  status=response.status_code, sent_parts=sent)
                sent += 1
                break
    return result(ok=True, sent_parts=sent, to=to)


def register(mcp) -> None:
    mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                          "idempotentHint": False, "openWorldHint": True})(telegram_send)
