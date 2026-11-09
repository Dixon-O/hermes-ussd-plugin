"""Africa's Talking SMS REST client. Outcome classification is the point of this module.

The caller needs one answer to "may I try this again?", and getting it wrong either loses a reply or
double-charges the user. Rules:

* Request provably never left (connect error/timeout): ``retry``.
* Request may have been processed but we never saw the answer (read timeout, dropped connection):
  ``unknown``. Never resent: a duplicate SMS costs money, a missing one is recoverable because the
  answer is stored and readable over USSD.
* The API answered: classify by HTTP status and per-recipient ``statusCode``.

No secret is ever placed in an exception message or log line from here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

import httpx

PROD_HOST = "api.africastalking.com"
SANDBOX_HOST = "api.sandbox.africastalking.com"
ALLOWED_HOSTS = frozenset({PROD_HOST, SANDBOX_HOST})
PATH = "/version1/messaging"

# Per-recipient statusCode -> outcome. 100-102 are accepted by Africa's Talking; the rest follow the
# documented table (verify against the live sandbox: see README "Verification status").
_OK = {100, 101, 102}
_BALANCE = {405}
_RETRY = {407, 500, 501, 502}
_PERMANENT = {401, 402, 403, 404, 406}


@dataclass(frozen=True)
class Outcome:
    kind: str  # sent | retry | permanent | auth | balance | unknown (probe_auth: ok | auth | retry | unknown)
    message_id: Optional[str] = None
    cost: Optional[str] = None
    detail: str = ""
    code: Optional[int] = None


def _safe(text: object, limit: int = 160) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s[:limit]


def parse_response(status: int, body: bytes | str) -> Outcome:
    """Classify one HTTP response from the messaging endpoint."""
    if status in (401, 403):
        return Outcome("auth", detail=f"HTTP {status}: credentials rejected")
    if 500 <= status < 600:
        return Outcome("retry", detail=f"HTTP {status}")
    if status == 429:
        return Outcome("retry", detail="HTTP 429: rate limited")
    if status >= 400:
        return Outcome("permanent", detail=f"HTTP {status}")
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
        data = json.loads(text)
        recipients = (data.get("SMSMessageData") or {}).get("Recipients")
    except (ValueError, AttributeError, TypeError):
        return Outcome("unknown", detail="HTTP 2xx with unreadable body")
    if not isinstance(recipients, list) or not recipients:
        message = _safe(((data or {}).get("SMSMessageData") or {}).get("Message")) if isinstance(data, dict) else ""
        return Outcome("permanent", detail=f"no recipients accepted: {message}")
    r = recipients[0] if isinstance(recipients[0], dict) else {}
    try:
        code = int(r.get("statusCode"))
    except (TypeError, ValueError):
        return Outcome("unknown", detail="HTTP 2xx without a recipient status code")
    mid = r.get("messageId")
    cost = r.get("cost")
    if code in _OK:
        return Outcome("sent", message_id=str(mid) if mid else None, cost=_safe(cost, 40) or None, code=code)
    status_text = _safe(r.get("status"))
    if code in _BALANCE:
        return Outcome("balance", detail=f"{code} {status_text}", code=code)
    if code in _RETRY:
        return Outcome("retry", detail=f"{code} {status_text}", code=code)
    if code in _PERMANENT or code >= 400:
        return Outcome("permanent", detail=f"{code} {status_text}", code=code)
    return Outcome("unknown", detail=f"unrecognised status {code}", code=code)


def classify_exception(exc: BaseException) -> Outcome:
    """Map a transport failure to retry (request never left) or unknown (it may have)."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return Outcome("retry", detail=f"connect failed: {type(exc).__name__}")
    return Outcome("unknown", detail=f"transport error after send: {type(exc).__name__}")


class ATClient:
    """Sends SMS through Africa's Talking.

    ``http`` is any ``httpx.AsyncClient``-like object. In production the adapter passes Hermes's
    SSRF-guarded ``platform_http_client``; tests pass a plain client aimed at a fake server and set
    ``base_url`` plus ``allow_hosts`` explicitly.
    """

    def __init__(self, username: str, api_key: str, *, sandbox: bool = True, sender: str = "",
                 http: Any = None, http_factory: Optional[Callable[[], Any]] = None,
                 base_url: Optional[str] = None, allow_hosts: Optional[frozenset] = None,
                 timeout: float = 15.0) -> None:
        self._username, self._api_key, self._sender = username, api_key, sender
        host = SANDBOX_HOST if sandbox else PROD_HOST
        self._url = base_url or f"https://{host}{PATH}"
        hosts = allow_hosts if allow_hosts is not None else ALLOWED_HOSTS
        if urlsplit(self._url).hostname not in hosts:
            raise ValueError("refusing to send to a host outside the Africa's Talking allowlist")
        self._http, self._factory, self._timeout = http, http_factory, timeout
        self._owns_http = False

    def _client(self) -> Any:
        if self._http is None:
            if self._factory is None:
                raise RuntimeError("ATClient needs http or http_factory")
            self._http, self._owns_http = self._factory(), True
        return self._http

    async def send_sms(self, to: str, message: str) -> Outcome:
        data = {"username": self._username, "to": to, "message": message}
        if self._sender:
            data["from"] = self._sender
        headers = {"apiKey": self._api_key, "Accept": "application/json"}
        try:
            resp = await self._client().post(
                self._url, data=data, headers=headers, timeout=self._timeout, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - classified, never propagated with details
            return classify_exception(exc)
        if 300 <= resp.status_code < 400:
            return Outcome("permanent", detail=f"unexpected redirect HTTP {resp.status_code}")
        return parse_response(resp.status_code, resp.content)

    async def probe_auth(self) -> Outcome:
        """Cheap credential check used by setup/doctor: asks for an SMS with no recipient.

        Africa's Talking answers a bad key with 401 and a good key with a 2xx/400 body about the
        missing recipient, so this does not send or charge anything.
        """
        try:
            resp = await self._client().post(
                self._url, data={"username": self._username, "to": "", "message": ""},
                headers={"apiKey": self._api_key, "Accept": "application/json"},
                timeout=self._timeout, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            return classify_exception(exc)
        if resp.status_code in (401, 403):
            return Outcome("auth", detail=f"HTTP {resp.status_code}: credentials rejected")
        return Outcome("ok", detail=f"credentials accepted (HTTP {resp.status_code})")

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            close = getattr(self._http, "aclose", None)
            if close is not None:
                result: Awaitable = close()
                await result
        self._http, self._owns_http = None, False
