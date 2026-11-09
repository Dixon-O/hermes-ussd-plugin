"""Store-first, send-second SMS delivery with at-most-once semantics. No Hermes imports.

Flow for one reply segment: row inserted ``queued`` -> claimed ``sending`` -> HTTP call -> final status.
The only way a row can end up ``unknown`` is a crash or an ambiguous network failure, and ``unknown``
is never retried: the answer is already stored, so the user can still read it over USSD.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional

from . import store as st
from .phone import mask
from .settings import Settings
from .sms.client import Outcome

log = logging.getLogger("hermes_ussd.outbox")

RETRY_DELAYS = (2.0, 8.0, 30.0)  # seconds before attempts 2, 3, 4; then failed_transient
AUTH_PAUSE = 300.0
BALANCE_PAUSE = 900.0
POLL = 5.0

SendFn = Callable[[str, str], Awaitable[Outcome]]


class Outbox:
    def __init__(self, store: st.Store, send: SendFn, settings: Settings, *,
                 clock: Callable[[], float] = time.time,
                 rand: Callable[[], float] = random.random) -> None:
        self.store, self._send, self.settings, self.clock, self._rand = store, send, settings, clock, rand
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self.paused_until = 0.0
        self.health = "ok"  # ok | auth | balance
        self.sent_total = 0

    # -- producer side ------------------------------------------------------
    def cap_reached(self) -> bool:
        cap = self.settings.daily_sms_cap
        if cap <= 0:
            return True
        return self.store.counter(st.day_key(self.clock(), self.settings.day_offset_hours)) >= cap

    def sent_today(self) -> int:
        return self.store.counter(st.day_key(self.clock(), self.settings.day_offset_hours))

    def enqueue(self, chat_id: str, ref: str, segments: list[str]) -> list[int]:
        ids = self.store.enqueue(chat_id, ref, segments)
        self.kick()
        return ids

    def kick(self) -> None:
        self._wake.set()

    # -- worker -------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="hermes-ussd-outbox")

    async def stop(self, drain_timeout: float = 5.0) -> None:
        self._stopping = True
        self.kick()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=drain_timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    log.debug("outbox worker cancelled during shutdown")
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                worked = await self.drain_once()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("outbox cycle failed")
                worked = 0
            if self._stopping:
                break
            if worked:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._sleep_for())
            except asyncio.TimeoutError:
                pass

    def _sleep_for(self) -> float:
        now = self.clock()
        t = self.store.next_due_at()
        wait = POLL if t is None else max(0.25, min(POLL, t - now))
        if self.paused_until > now:
            wait = max(wait, min(POLL, self.paused_until - now))
        return wait

    async def drain_once(self, limit: int = 20) -> int:
        """Process due rows; returns how many were handled. Public so one-shot senders can use it."""
        if self.clock() < self.paused_until:
            return 0
        handled = 0
        for row in self.store.due(limit):
            if self._stopping and handled:
                break
            if self.clock() < self.paused_until:
                break
            if await self._process(row):
                handled += 1
        return handled

    async def _process(self, row: st.OutboxRow) -> bool:
        prior = self.store.prior_segments_ok(row)
        if prior is None:
            return False  # earlier segment still pending: keep order
        if prior is False:
            self.store.finish(row.id, "failed_permanent", error="earlier segment failed")
            return True
        if self.store.is_opted_out(row.chat_id):
            self.store.finish(row.id, "skipped_optout", error="recipient opted out")
            return True
        if self.cap_reached():
            self.store.finish(row.id, "failed_cap", error="daily SMS cap reached")
            return True
        if not self.store.mark_sending(row.id):
            return False
        outcome = await self._send(row.chat_id, row.body)
        self._apply(row, outcome)
        return True

    def _apply(self, row: st.OutboxRow, o: Outcome) -> None:
        attempts = row.attempts + 1
        if o.kind == "sent":
            self.store.incr(st.day_key(self.clock(), self.settings.day_offset_hours))
            self.store.finish(row.id, "sent", message_id=o.message_id, cost=o.cost)
            self.sent_total += 1
            if self.health != "ok":
                log.info("SMS delivery recovered")
            self.health = "ok"
        elif o.kind == "retry":
            if attempts > len(RETRY_DELAYS):
                self.store.finish(row.id, "failed_transient", error=o.detail)
                log.warning("SMS to %s failed after %d attempts: %s", mask(row.chat_id), attempts, o.detail)
            else:
                delay = RETRY_DELAYS[attempts - 1] * (0.8 + 0.4 * self._rand())
                self.store.finish(row.id, "queued", error=o.detail, retry_at=self.clock() + delay)
        elif o.kind == "auth":
            self.health = "auth"
            self.paused_until = self.clock() + AUTH_PAUSE
            self.store.finish(row.id, "queued", error=o.detail, retry_at=self.paused_until, refund_attempt=True)
            log.error("Africa's Talking rejected the credentials; pausing SMS for %ds. Check AT_API_KEY/AT_USERNAME.",
                      int(AUTH_PAUSE))
        elif o.kind == "balance":
            self.health = "balance"
            self.paused_until = self.clock() + BALANCE_PAUSE
            self.store.finish(row.id, "queued", error=o.detail, retry_at=self.paused_until, refund_attempt=True)
            log.error("Africa's Talking balance is too low; pausing SMS for %ds.", int(BALANCE_PAUSE))
        elif o.kind == "permanent":
            self.store.finish(row.id, "failed_permanent", error=o.detail)
            log.warning("SMS to %s rejected: %s", mask(row.chat_id), o.detail)
        else:  # unknown: may have been delivered; never resend
            self.store.finish(row.id, "unknown", error=o.detail)
            log.warning("SMS to %s outcome unknown (%s); not retrying", mask(row.chat_id), o.detail)
