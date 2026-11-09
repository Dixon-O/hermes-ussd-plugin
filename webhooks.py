"""aiohttp routes for the Africa's Talking callbacks, with every guard in front of the service.

Africa's Talking does not sign its callbacks, so the *only* authentication is a long random path
segment: ``/<AT_WEBHOOK_SECRET>/ussd`` (and ``/sms``, ``/dlr``). Consequences, all enforced here:

* the secret is compared in constant time;
* every rejection (wrong secret, wrong method, wrong content type, oversized/slow body, missing
  fields) is the same bare ``404``: nothing tells a scanner the endpoint exists;
* access logging is disabled by the caller, because the access log would print the secret;
* request paths and bodies are never logged by this module.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Awaitable, Callable, Mapping

from aiohttp import web

from .service import Service
from .ussd import render
from .ussd.strings import t

log = logging.getLogger("hermes_ussd.webhooks")

MAX_BODY = 4096
READ_TIMEOUT = 2.0
USSD_DEADLINE = 6.0  # Africa's Talking gives ~10 s; answer well inside it
FORM_TYPE = "application/x-www-form-urlencoded"


def _not_found() -> web.Response:
    return web.Response(status=404, text="", content_type="text/plain")


def secret_matches(candidate: str, secret: str) -> bool:
    """Constant-time comparison that is also safe for non-ASCII input."""
    return hmac.compare_digest(candidate.encode("utf-8", "replace"), secret.encode("utf-8", "replace"))


async def _read_form(request: web.Request) -> Mapping[str, str] | None:
    """The parsed urlencoded body, or ``None`` if it violates any guard."""
    if request.method != "POST":
        return None
    ctype = (request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != FORM_TYPE:
        return None
    if request.content_length is not None and request.content_length > MAX_BODY:
        return None
    try:
        raw = await asyncio.wait_for(request.content.read(MAX_BODY + 1), timeout=READ_TIMEOUT)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return None
    if len(raw) > MAX_BODY:
        return None
    from urllib.parse import parse_qs

    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
    except (UnicodeDecodeError, ValueError):
        return None
    return {k: v[0] for k, v in parsed.items() if v}


def make_app(service: Service, secret: str) -> web.Application:
    """Build the application. The caller runs it with ``access_log=None``."""
    app = web.Application(client_max_size=MAX_BODY)

    def guarded(kind: str):
        async def handler(request: web.Request) -> web.Response:
            if not secret_matches(request.match_info.get("secret", ""), secret):
                return _not_found()
            form = await _read_form(request)
            if form is None:
                return _not_found()
            if kind == "ussd":
                if not form.get("sessionId") or not form.get("phoneNumber"):
                    return _not_found()
                try:
                    body = await asyncio.wait_for(service.ussd(form), timeout=USSD_DEADLINE)
                except asyncio.TimeoutError:
                    body = render.end(t(service.settings.default_language, "unavailable"))
                except Exception:  # noqa: BLE001
                    log.exception("ussd handler crashed")
                    body = render.end(t(service.settings.default_language, "error"))
                if not render.is_valid_screen(body):  # last line of defence: never emit a bad reply
                    body = render.end(t(service.settings.default_language, "error"))
                return web.Response(text=body, content_type="text/plain", charset="utf-8")
            fn: Callable[[Mapping[str, str]], Awaitable[None]] = service.sms_in if kind == "sms" else service.dlr
            try:
                await asyncio.wait_for(fn(form), timeout=USSD_DEADLINE)
            except Exception:  # noqa: BLE001
                log.exception("%s handler crashed", kind)
            return web.Response(status=200, text="", content_type="text/plain")

        return handler

    # Any method is routed so that non-POST gets the same bare 404 as everything else.
    for kind in ("ussd", "sms", "dlr"):
        app.router.add_route("*", "/{secret}/" + kind, guarded(kind))

    async def anything_else(request: web.Request) -> web.Response:
        return _not_found()  # same bytes as every other rejection: no fingerprint for scanners

    app.router.add_route("*", "/{tail:.*}", anything_else)
    return app


async def serve(app: web.Application, host: str, port: int) -> tuple[web.AppRunner, int]:
    """Start ``app`` and return ``(runner, bound_port)``.

    The single place the listener is created, so the safety settings cannot be forgotten:
    ``access_log=None`` because aiohttp's access log prints the request path, which here *is* the
    credential. ``handle_signals=False`` because the gateway owns signal handling.
    """
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, host, port, reuse_address=True)
    try:
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner, int(runner.addresses[0][1])
