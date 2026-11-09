"""Out-of-process delivery: ``hermes send --to africastalking:+254...`` and cron ``deliver=``.

These run in a process with no live gateway, so this module opens the same database and uses the same
outbox, which keeps the daily cap, opt-outs and idempotency identical to the live path.

The target validator is a security control, not a convenience: a prompt-injected agent could
otherwise schedule a cron job that texts arbitrary numbers at the operator's expense. Outbound
messages may only go to the allowlist or the home channel.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from gateway.platforms._shared import get_scoped_secret, send_error
from gateway.platforms.helpers import strip_markdown

from . import adapter as _adapter
from .outbox import Outbox
from .phone import DEFAULT_COUNTRY_CODE, normalize_e164, parse_allowlist
from .settings import ConfigError, load_settings, truthy
from .sms import encoding

DRAIN_DEADLINE = 25.0


def _get(name: str) -> Optional[str]:
    return get_scoped_secret(name, None)


def _policy() -> tuple[frozenset, bool, str, str]:
    """(allowlist, allow_all, home_channel, country code) read from env without needing secrets."""
    cc = "".join(ch for ch in str(_get("AT_DEFAULT_COUNTRY_CODE") or "") if ch.isdigit()) or DEFAULT_COUNTRY_CODE
    home = normalize_e164(_get("AT_HOME_CHANNEL") or "", cc) or ""
    return parse_allowlist(_get("AT_ALLOWED_USERS") or "", cc), truthy(_get("AT_ALLOW_ALL_USERS")), home, cc


def parse_target(raw: str) -> Optional[tuple[str, Optional[str]]]:
    """Accept a phone number as a target. Returns ``(chat_id, None)`` or ``None`` to decline."""
    n = normalize_e164((raw or "").strip(), _policy()[3])
    return (n, None) if n else None


def validate_target(address: str) -> bool | str:
    """True when texting ``address`` is permitted; otherwise a reason."""
    allowed, allow_all, home, cc = _policy()
    n = normalize_e164(address, cc)
    if n is None:
        return "not a valid phone number (use E.164, e.g. +254700000001)"
    if allow_all or n in allowed or n == home:
        return True
    return "number is not in AT_ALLOWED_USERS or AT_HOME_CHANNEL"


async def standalone_send(pconfig: Any, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                          media_files: Any = None, force_document: bool = False, **_: Any) -> dict:
    """Send ``message`` without a running gateway. Never raises; returns the dispatcher's envelope."""
    try:
        settings = load_settings(_get, getattr(pconfig, "extra", None))
    except ConfigError as exc:
        return send_error(f"africastalking is not configured: {exc}")
    ok = validate_target(chat_id)
    if ok is not True:
        return send_error(f"refusing to text {chat_id!r}: {ok}")
    number = normalize_e164(chat_id, settings.default_country_code)
    plain = strip_markdown(message or "")
    split = encoding.split_segments(plain, settings.sms_max_segments, truncation_note=_adapter.TRUNCATION_NOTE)
    if not split.segments:
        return {"success": True, "platform": "africastalking", "chat_id": number, "message_id": "empty"}
    store = client = None
    try:
        store = _adapter.open_store()
        client = _adapter.make_client(settings)
        outbox = Outbox(store, client.send_sms, settings)
        ref = store.new_ref()
        store.put_reply(ref, number, encoding.normalize(plain), split.truncated, len(split.segments))
        outbox.enqueue(number, ref, list(split.segments))
        deadline = time.monotonic() + DRAIN_DEADLINE
        while time.monotonic() < deadline:
            await outbox.drain_once()
            if store.queued_count(number) == 0:
                break
            await asyncio.sleep(0.5)
        counts = {r["status"]: r["n"] for r in store.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox WHERE chat_id=? AND ref=? GROUP BY status", (number, ref))}
        if counts.get("sent", 0) == len(split.segments):
            return {"success": True, "platform": "africastalking", "chat_id": number, "message_id": ref}
        return send_error(f"SMS not fully delivered: {counts or 'nothing sent'}")
    except Exception as exc:  # noqa: BLE001 - dispatcher also catches, but keep our own clean message
        return send_error(f"SMS send failed: {type(exc).__name__}")
    finally:
        if client is not None:
            await client.aclose()
        if store is not None:
            store.close()
