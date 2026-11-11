"""The Hermes gateway adapter: glues the pure/service layers to BasePlatformAdapter.

Imports Hermes only through its documented public surface (gateway.platforms.base/event/helpers/
_shared, gateway.config, plugins.plugin_storage). Everything with real logic lives in modules that
do not import Hermes, so it is tested without a gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import env_is_connected, get_scoped_secret, seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import platform_http_client, strip_markdown

from . import store as st
from . import webhooks
from .outbox import Outbox
from .phone import mask, normalize_e164
from .service import Service
from .settings import DEFAULT_TOOLSETS, ConfigError, Settings, load_settings
from .sms import encoding
from .sms.client import ATClient

log = logging.getLogger("hermes_ussd")

PLUGIN_NAME = "hermes-ussd"
PLATFORM_NAME = "africastalking"

# Cached into the system prompt: keep stable.
PLATFORM_HINT = (
    "You are replying by SMS to a person who may be using a basic feature phone and paying per message. "
    "Use plain text only: no markdown, no emoji, no tables, no links unless essential. "
    "Put the key point first and keep the whole reply under 300 characters when you can; never exceed 450. "
    "Do not ask the user to open an app, view an image or click a button. "
    "The user may answer with just a number or a word; read it as a reply to your last message."
)

TRUNCATION_NOTE = "... (rest: dial the USSD code, choose 2)"
# Gateway housekeeping (busy acks, drain notices, restart pings) all open with one of these
# language-independent markers. They must never cost the user an SMS.
_NOTICE_MARKERS = ("⚡", "⏳", "⏩", "↪", "♻", "\U0001f4be", "\U0001f4ec", "◐", "⚠", "❌")
_NOTICE_MAX_LEN = 600


def _get(name: str) -> Optional[str]:
    return get_scoped_secret(name, None)


def _deps_ok() -> bool:
    try:
        import aiohttp  # noqa: F401
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


def check_requirements() -> bool:
    """Passive probe: dependencies importable and the three required values present."""
    return _deps_ok() and all((get_scoped_secret(n, "") or "").strip() for n in
                              ("AT_USERNAME", "AT_API_KEY", "AT_WEBHOOK_SECRET"))


def validate_config(config: Any) -> bool:
    try:
        load_settings(_get, getattr(config, "extra", None))
    except ConfigError:
        return False
    return True


is_connected = env_is_connected("AT_USERNAME", "AT_API_KEY", "AT_WEBHOOK_SECRET")


def env_enablement() -> Optional[dict]:
    """Seed non-secret settings so the gateway auto-enables the platform from env alone."""
    if not check_requirements():
        return None
    extra = seed_extra_from_env(
        (("AT_ENV", "env", None), ("AT_WEBHOOK_PORT", "port", int), ("AT_USSD_CODE", "ussd_code", None)),
        home_env="AT_HOME_CHANNEL",
    )
    if isinstance(extra, dict):
        # Gateway setup/operational notices ("no home channel is set...") go through send_private_notice,
        # which this adapter drops: nobody should pay for an SMS about gateway housekeeping.
        extra.setdefault("notice_delivery", "private")
    return extra


def is_notice(text: str) -> bool:
    """True for gateway housekeeping text that must not be sent as an SMS."""
    head = text.lstrip()[:1]
    return bool(head) and head in _NOTICE_MARKERS and len(text) <= _NOTICE_MAX_LEN


# Hermes reports a failed model call as ordinary reply text. Texting that to a phone costs money and
# shows the user provider internals, so it is replaced by one generic line (and sent once per ask).
_FAILURE_STEMS = ("retrying won't help", "provider said:", "switch models or start a new session",
                  "no response from provider", "pick another model with /model", "hermes/logs/agent.log")
FAILURE_REPLY = "Sorry, I could not answer that just now. Please try again in a few minutes."


def is_failure_copy(text: str) -> bool:
    """True for Hermes's own failed-turn copy (provider errors, timeouts), which must not reach a phone."""
    low = text.lower()
    return len(text) <= 1500 and any(stem in low for stem in _FAILURE_STEMS)


def open_store() -> st.Store:
    """Open (and migrate) the plugin database under the profile's plugin-data directory."""
    from plugins.plugin_storage import plugin_db

    conn = plugin_db(PLUGIN_NAME)
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if path:
            os.chmod(os.path.dirname(path), 0o700)
            os.chmod(path, 0o600)
    except (OSError, IndexError, TypeError):
        pass  # best effort: not every filesystem supports modes
    store = st.Store(conn)
    store.migrate()
    return store


def make_client(settings: Settings) -> ATClient:
    return ATClient(
        settings.username, settings.api_key, sandbox=settings.sandbox, sender=settings.sms_sender,
        http_factory=lambda: platform_http_client(timeout=15.0, follow_redirects=False))


class ATAdapter(BasePlatformAdapter):
    """USSD in, SMS out. One listener, one SQLite file, one outbox worker."""

    MAX_MESSAGE_LENGTH = 459
    splits_long_messages = True  # send() does its own GSM-7 splitting and truncation

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        self._settings: Optional[Settings] = None
        self._store: Optional[st.Store] = None
        self._outbox: Optional[Outbox] = None
        self._service: Optional[Service] = None
        self._client: Optional[ATClient] = None
        self._runner = None
        self._purge_task: Optional[asyncio.Task] = None
        self.port: Optional[int] = None
        # Texts to people who never asked for one: no restart pings, no typing indicator.
        for attr in ("gateway_restart_notification", "typing_indicator"):
            try:
                setattr(config, attr, False)
            except Exception:  # noqa: BLE001
                log.debug("could not set %s on the platform config", attr)

    # -- lifecycle ------------------------------------------------------------------------------
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            self._settings = load_settings(_get, self.config.extra)
        except ConfigError as exc:
            log.error("[africastalking] %s", exc)
            self._set_fatal_error("africastalking_config", f"[africastalking] {exc}", retryable=False)
            return False
        s = self._settings
        if not self._acquire_platform_lock(PLATFORM_NAME, f"{s.username}:{s.port}", "Africa's Talking webhook port"):
            return False
        try:
            self._store = open_store()
            recovered = self._store.recover_sending()
            if recovered:
                log.warning("[africastalking] %d SMS were mid-send at the last shutdown; marked 'unknown', not resent",
                            recovered)
            self._client = make_client(s)
            self._outbox = Outbox(self._store, self._client.send_sms, s)
            self._service = Service(self._store, self._outbox, s, self._submit_turn)
            self._runner, self.port = await webhooks.serve(
                webhooks.make_app(self._service, s.webhook_secret), s.host, s.port)
            self._outbox.start()
            self._purge_task = asyncio.create_task(self._purge_loop(), name="hermes-ussd-purge")
        except OSError as exc:
            log.error("[africastalking] cannot listen on %s:%s: %s", s.host, s.port, exc.strerror or exc)
            self._set_fatal_error("africastalking_bind", f"cannot listen on {s.host}:{s.port}: {exc.strerror or exc}",
                                  retryable=True)
            await self._teardown()
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("[africastalking] failed to start")
            self._set_fatal_error("africastalking_start", f"failed to start: {type(exc).__name__}", retryable=True)
            await self._teardown()
            return False
        self._mark_connected(listener_base=f"http://{s.host}:{self.port}")
        self._wire_plugin_handlers(None)
        log.info("[africastalking] listening on %s:%s (%s); allowlist=%d number(s)%s",
                 s.host, self.port, "SANDBOX" if s.sandbox else "PRODUCTION", len(s.allowed),
                 ", open to everyone" if s.allow_all else "")
        if not s.allowed and not s.allow_all:
            log.warning("[africastalking] AT_ALLOWED_USERS is empty: every caller is refused. "
                        "Set it to your E.164 numbers (or AT_ALLOW_ALL_USERS=true to open the service).")
        return True

    async def disconnect(self) -> None:
        await self._teardown()
        self._mark_disconnected()
        log.info("[africastalking] disconnected")

    async def _teardown(self) -> None:
        if self._purge_task is not None:
            self._purge_task.cancel()
            try:
                await self._purge_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                log.debug("purge task cancelled during shutdown")
            self._purge_task = None
        if self._runner is not None:  # stop taking calls before draining what is already queued
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                log.debug("listener cleanup failed", exc_info=True)
            self._runner = None
        if self._outbox is not None:
            await self._outbox.stop(5.0)
        if self._client is not None:
            await self._client.aclose()
        if self._store is not None:
            self._store.close()
        self._outbox = self._service = self._client = self._store = None
        self._release_platform_lock()

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                if self._store and self._settings:
                    removed = self._store.purge(self._settings.retention_days)
                    if any(removed.values()):
                        log.info("[africastalking] retention purge: %s", removed)
            except Exception:  # noqa: BLE001
                log.exception("retention purge failed")

    # -- inbound: hand a turn to the gateway ----------------------------------------------------
    def _submit_turn(self, chat_id: str, text: str, origin: str, message_id: str, allow_control: bool) -> None:
        source = self.build_source(
            chat_id=chat_id, chat_name=mask(chat_id), chat_type="dm", user_id=chat_id,
            user_name=mask(chat_id), message_id=message_id)
        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, raw_message={"origin": origin},
            message_id=message_id, allow_gateway_control=allow_control)
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(lambda t, c=chat_id: self._turn_done(t, c))

    def _turn_done(self, task: asyncio.Task, chat_id: str) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("[africastalking] turn for %s failed: %s", mask(chat_id), type(exc).__name__)
            apology = asyncio.create_task(self.send(chat_id, "Sorry, something went wrong. Please try again."))
            self._background_tasks.add(apology)
            apology.add_done_callback(self._background_tasks.discard)

    # -- outbound -------------------------------------------------------------------------------
    async def send_private_notice(self, chat_id: str, user_id: Optional[str], content: str,
                                  reply_to: Optional[str] = None,
                                  metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Gateway setup/operational notices never become SMS (they cost money and mean nothing on a phone)."""
        return SendResult(success=True, message_id="suppressed-notice")

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        s, store, outbox = self._settings, self._store, self._outbox
        if s is None or store is None or outbox is None:
            return SendResult(success=False, error="africastalking adapter is not connected", retryable=True)
        text = content or ""
        if is_notice(text):
            return SendResult(success=True, message_id="suppressed-notice")
        number = normalize_e164(chat_id, s.default_country_code)
        if number is None:
            return SendResult(success=False, error="invalid recipient number", error_kind="not_found")
        failure = is_failure_copy(text)
        claimed = store.claim_oldest_ask(number)
        if failure:
            if claimed is None:  # a second error line for an ask already answered: say nothing
                return SendResult(success=True, message_id="suppressed-notice")
            log.warning("agent turn failed for %s; sending a generic reply instead of the provider error",
                        mask(number))
            text = FAILURE_REPLY
        plain = strip_markdown(text)
        ref, origin = claimed if claimed else (store.new_ref(), "system")
        prefix = f"Ref {ref}: " if origin == "ussd" else ""
        split = encoding.split_segments(prefix + plain, s.sms_max_segments, truncation_note=TRUNCATION_NOTE)
        if not split.segments:
            return SendResult(success=True, message_id="empty")
        # Store first: the full answer is readable over USSD whatever happens to the SMS.
        store.put_reply(ref, number, encoding.normalize(plain), split.truncated, len(split.segments))
        if store.is_opted_out(number):
            return SendResult(success=True, message_id=f"stored:{ref}")
        outbox.enqueue(number, ref, list(split.segments))
        return SendResult(success=True, message_id=f"at:{ref}")

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        return None  # USSD/SMS have no typing indicator

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": mask(chat_id), "type": "dm"}

    def format_message(self, content: str) -> str:
        return strip_markdown(content)

    def toolsets_for_source(self, source: Any) -> Optional[list[str]]:
        """Phone sessions get a restricted toolset by default (no terminal, no files). Fails closed
        if settings are not loaded; ``AT_TOOLSETS=default`` hands control back to the gateway."""
        s = self._settings
        if s is None:
            return list(DEFAULT_TOOLSETS)
        return list(s.toolsets) if s.toolsets else None

    # -- introspection (used by tests and `hermes ussd status`) ---------------------------------
    def health(self) -> dict[str, Any]:
        out = self._outbox
        return {"connected": self._runner is not None, "port": self.port,
                "sms_health": out.health if out else "n/a", "sent_today": out.sent_today() if out else 0}
