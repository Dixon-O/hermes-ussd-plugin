"""Business logic behind the three Africa's Talking webhooks. No Hermes imports, no HTTP framework.

Everything the internet can reach funnels through :class:`Service`. Handlers return what the gateway
needs (a USSD screen) or nothing (SMS/DLR), never raise, and never log message text or full numbers.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Mapping, Optional

from . import store as st
from .limits import Limiter, per_hour, per_minute
from .outbox import Outbox
from .phone import mask, normalize_e164
from .settings import Settings
from .sms.encoding import split_segments
from .ussd import menu, render
from .ussd.strings import normalize_lang, t

log = logging.getLogger("hermes_ussd.service")

MAX_HOP_TEXT = 400
MAX_SESSION_ID = 100
STOP_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
START_WORDS = frozenset({"START", "UNSTOP"})  # not "YES": that is a normal reply to the agent
HELP_WORDS = frozenset({"HELP", "INFO"})

# (chat_id, text, origin, message_id, allow_control) -> None. Must return quickly (schedule the turn).
SubmitFn = Callable[[str, str, str, str, bool], None]


def _field(form: Mapping[str, Any], name: str, limit: int) -> str:
    v = form.get(name, "")
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    return str(v)[:limit] if v is not None else ""


class Service:
    def __init__(self, store: st.Store, outbox: Outbox, settings: Settings, submit: SubmitFn, *,
                 hop_limiter: Optional[Limiter] = None, ask_limiter: Optional[Limiter] = None,
                 help_limiter: Optional[Limiter] = None) -> None:
        self.store, self.outbox, self.settings, self._submit = store, outbox, settings, submit
        self.hops = hop_limiter or per_minute(settings.hops_per_minute)
        self.asks = ask_limiter or per_hour(settings.per_number_asks_per_hour)
        self.helps = help_limiter or per_hour(1)

    # -- access control -----------------------------------------------------
    def allowed(self, number: str) -> bool:
        """Open only by explicit opt-in or allowlist. No allowlist and no opt-in means closed, so a
        stranger can never trigger an agent turn (or a pairing SMS) on this number's bill."""
        s = self.settings
        return s.allow_all or number in s.allowed

    # -- USSD ---------------------------------------------------------------
    async def ussd(self, form: Mapping[str, Any]) -> str:
        """The complete USSD reply (``CON ...``/``END ...``) for one hop."""
        lang = self.settings.default_language
        try:
            number = normalize_e164(_field(form, "phoneNumber", 32), self.settings.default_country_code)
            session = _field(form, "sessionId", MAX_SESSION_ID)
            if not number or not session:
                return render.end(t(lang, "error"))
            lang = normalize_lang(self.store.get_lang(number) or self.settings.default_language)
            if not self.allowed(number):
                log.info("ussd: refused non-allowlisted %s", mask(number))
                return render.end(t(lang, "private"))
            if not self.hops.allow(f"hop:{number}"):
                return render.end(t(lang, "rate_limited"))
            text = _field(form, "text", MAX_HOP_TEXT)
            ctx = _Ctx(
                lang=lang, last_answer=self.store.last_reply(number),
                pending=self.store.pending_asks(number) + self.store.queued_count(number),
                sent_today=self.outbox.sent_today(), daily_cap=self.settings.daily_sms_cap)
            step = menu.step(text, ctx)
            if step.set_lang:
                self.store.set_lang(number, step.set_lang)
                return step.screen
            if step.ask:
                return self._ask_via_ussd(number, session, text, step.ask, lang)
            return step.screen
        except Exception:  # noqa: BLE001 - a USSD reply must always be produced
            log.exception("ussd hop failed")
            return render.end(t(lang, "error"))

    def _ask_via_ussd(self, number: str, session: str, path: str, question: str, lang: str) -> str:
        if self.store.is_opted_out(number):
            return render.end(t(lang, "opted_out"))
        if not self.asks.allow(f"ask:{number}"):
            return render.end(t(lang, "rate_limited"))
        digest = hashlib.sha256(path.encode()).hexdigest()[:32]
        fresh_ref = self.store.new_ref()
        is_new, ref = self.store.claim_inbound(f"ussd:{session}:{digest}", fresh_ref)
        ref = ref or fresh_ref
        if is_new:
            self.store.add_ask(ref, number, "ussd")
            # USSD text is typed on a feature phone: never let it act as a gateway command.
            self._submit(number, question, "ussd", f"ussd:{ref}", False)
        if self.outbox.cap_reached():
            return render.end(t(lang, "cap_reached"))
        return menu.ask_receipt(lang, ref)

    # -- inbound SMS --------------------------------------------------------
    async def sms_in(self, form: Mapping[str, Any]) -> None:
        try:
            number = normalize_e164(_field(form, "from", 32), self.settings.default_country_code)
            text = _field(form, "text", 1000).strip()
            if not number or not text:
                return
            mid = _field(form, "id", 100) or hashlib.sha256(
                f"{number}|{text}|{_field(form, 'date', 40)}".encode()).hexdigest()[:32]
            is_new, _ = self.store.claim_inbound(f"sms:{mid}")
            if not is_new:
                return
            word = text.upper()
            lang = normalize_lang(self.store.get_lang(number) or self.settings.default_language)
            if word in STOP_WORDS:
                self.store.opt_out(number)
                log.info("sms: %s opted out", mask(number))
                return
            if word in START_WORDS:
                self.store.opt_in(number)
                if self.allowed(number):
                    self._reply_direct(number, _START_ACK[lang])
                return
            if not self.allowed(number):
                log.info("sms: ignored non-allowlisted %s", mask(number))
                return
            if self.store.is_opted_out(number):
                return
            if word in HELP_WORDS:
                if self.helps.allow(f"help:{number}"):
                    self._reply_direct(number, _strip_nav(t(lang, "help")))
                return
            if not self.asks.allow(f"ask:{number}"):
                return
            ref = self.store.new_ref()
            self.store.add_ask(ref, number, "sms")
            self._submit(number, text, "sms", f"sms:{mid}", self.settings.allow_commands)
        except Exception:  # noqa: BLE001
            log.exception("inbound sms failed")

    def _reply_direct(self, number: str, text: str) -> None:
        """A system-originated SMS (help/START ack). Counts against the same cap and outbox."""
        ref = self.store.new_ref()
        split = split_segments(text, 1)
        if split.segments:
            self.outbox.enqueue(number, ref, list(split.segments))

    # -- delivery reports ---------------------------------------------------
    async def dlr(self, form: Mapping[str, Any]) -> None:
        try:
            mid = _field(form, "id", 100)
            status = _field(form, "status", 40)
            if mid and status:
                self.store.apply_dlr(mid, status, _field(form, "failureReason", 120) or None)
        except Exception:  # noqa: BLE001
            log.exception("delivery report failed")


_START_ACK = {"en": "You are subscribed again. Reply STOP to opt out.",
              "sw": "Umejiunga tena. Tuma STOP kujiondoa."}


def _strip_nav(text: str) -> str:
    """Drop the trailing '0 Back' line from a USSD help string for SMS use."""
    lines = [ln for ln in text.split("\n") if not ln.strip().startswith("0 ")]
    return " ".join(lines)


class _Ctx:
    """Read-only snapshot handed to the pure menu."""

    __slots__ = ("lang", "last_answer", "pending", "sent_today", "daily_cap")

    def __init__(self, lang: str, last_answer: Optional[str], pending: int, sent_today: int, daily_cap: int):
        self.lang, self.last_answer, self.pending = lang, last_answer, pending
        self.sent_today, self.daily_cap = sent_today, daily_cap
