import asyncio

import pytest

from hermes_ussd import store as st
from hermes_ussd.outbox import AUTH_PAUSE, BALANCE_PAUSE, RETRY_DELAYS, Outbox
from hermes_ussd.settings import load_settings
from hermes_ussd.sms.client import Outcome

from conftest import SECRET

A = "+254700000001"


def mk_settings(**over):
    env = {"AT_USERNAME": "u", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET}
    env.update(over)
    return load_settings(env.get)


class Script:
    """A scripted sender: pops outcomes in order, repeats the last one."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def __call__(self, to, body):
        self.calls.append((to, body))
        o = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return o


def box(store, clock, send, **settings):
    return Outbox(store, send, mk_settings(**settings), clock=clock, rand=lambda: 0.5)


SENT = Outcome("sent", message_id="m1", cost="KES 1")


async def test_happy_path_sends_segments_in_order(store, clock):
    send = Script(SENT)
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["seg1", "seg2", "seg3"])
    for _ in range(5):
        await ob.drain_once()
    assert [b for _, b in send.calls] == ["seg1", "seg2", "seg3"]
    assert store.status_counts() == {"sent": 3} and ob.sent_today() == 3 and ob.sent_total == 3


async def test_retry_schedule_then_success(store, clock):
    send = Script(Outcome("retry", detail="HTTP 500"), Outcome("retry", detail="HTTP 500"), SENT)
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["x"])
    await ob.drain_once()
    assert store.status_counts() == {"queued": 1} and len(send.calls) == 1
    await ob.drain_once()
    assert len(send.calls) == 1  # not due yet: backoff is honoured
    clock.advance(RETRY_DELAYS[0] * 1.1)
    await ob.drain_once()
    clock.advance(RETRY_DELAYS[1] * 1.1)
    await ob.drain_once()
    assert store.status_counts() == {"sent": 1} and len(send.calls) == 3


async def test_gives_up_after_four_attempts(store, clock):
    send = Script(Outcome("retry", detail="HTTP 503"))
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["x"])
    for _ in range(10):
        await ob.drain_once()
        clock.advance(60)
    assert store.status_counts() == {"failed_transient": 1}
    assert len(send.calls) == len(RETRY_DELAYS) + 1


async def test_permanent_failure_skips_remaining_segments(store, clock):
    send = Script(Outcome("permanent", detail="406 UserInBlacklist"))
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["a", "b", "c"])
    for _ in range(4):
        await ob.drain_once()
    assert len(send.calls) == 1
    assert store.status_counts() == {"failed_permanent": 3}


async def test_unknown_outcome_is_never_retried(store, clock):
    send = Script(Outcome("unknown", detail="transport error after send"))
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["x"])
    for _ in range(5):
        await ob.drain_once()
        clock.advance(1000)
    assert len(send.calls) == 1 and store.status_counts() == {"unknown": 1}


async def test_auth_failure_pauses_and_keeps_row_queued(store, clock):
    send = Script(Outcome("auth", detail="HTTP 401"), SENT)
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["x"])
    await ob.drain_once()
    assert ob.health == "auth" and store.status_counts() == {"queued": 1}
    assert await ob.drain_once() == 0 and len(send.calls) == 1  # circuit open: no hammering
    clock.advance(AUTH_PAUSE + 1)
    await ob.drain_once()
    assert store.status_counts() == {"sent": 1} and ob.health == "ok"
    assert store.conn.execute("SELECT attempts FROM outbox").fetchone()[0] == 1  # the pause cost no attempt


async def test_balance_failure_pauses_longer(store, clock):
    send = Script(Outcome("balance", detail="405"), SENT)
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["x"])
    await ob.drain_once()
    assert ob.health == "balance"
    clock.advance(AUTH_PAUSE + 1)
    assert await ob.drain_once() == 0
    clock.advance(BALANCE_PAUSE)
    await ob.drain_once()
    assert store.status_counts() == {"sent": 1}


async def test_daily_cap_blocks_and_marks(store, clock):
    send = Script(SENT)
    ob = box(store, clock, send, AT_DAILY_SMS_CAP="2")
    ob.enqueue(A, "1", ["a", "b", "c"])
    for _ in range(5):
        await ob.drain_once()
    assert len(send.calls) == 2
    assert store.status_counts() == {"sent": 2, "failed_cap": 1}
    assert ob.cap_reached()


async def test_cap_resets_next_local_day(store, clock):
    send = Script(SENT)
    ob = box(store, clock, send, AT_DAILY_SMS_CAP="1")
    ob.enqueue(A, "1", ["a"])
    await ob.drain_once()
    assert ob.cap_reached()
    clock.advance(86400)
    assert not ob.cap_reached()


async def test_zero_cap_disables_sending(store, clock):
    ob = box(store, clock, Script(SENT), AT_DAILY_SMS_CAP="0")
    assert ob.cap_reached()


async def test_opted_out_recipient_is_never_texted(store, clock):
    send = Script(SENT)
    ob = box(store, clock, send)
    store.opt_out(A)
    ob.enqueue(A, "1", ["x"])
    await ob.drain_once()
    assert send.calls == [] and store.status_counts() == {"skipped_optout": 1}


async def test_crash_recovery_never_resends(store, clock):
    send = Script(SENT)
    ob = box(store, clock, send)
    [rid] = ob.enqueue(A, "1", ["x"])
    assert store.mark_sending(rid)  # claimed, then the process "crashes" before finishing
    assert store.recover_sending() == 1
    await ob.drain_once()
    assert send.calls == [] and store.status_counts() == {"unknown": 1}


async def test_replayed_enqueue_sends_once(store, clock):
    send = Script(SENT)
    ob = box(store, clock, send)
    ob.enqueue(A, "1", ["x"])
    ob.enqueue(A, "1", ["x"])
    for _ in range(3):
        await ob.drain_once()
    assert len(send.calls) == 1


async def test_two_workers_cannot_double_send(store, clock):
    send = Script(SENT)
    one, two = box(store, clock, send), box(store, clock, send)
    one.enqueue(A, "1", ["x"])
    await asyncio.gather(*(w.drain_once() for w in (one, two)))
    assert len(send.calls) == 1


async def test_worker_task_delivers_and_stops_cleanly(store, clock):
    send = Script(SENT)
    ob = box(store, clock, send)
    ob.start()
    ob.enqueue(A, "1", ["x", "y"])
    for _ in range(100):
        if len(send.calls) == 2:
            break
        await asyncio.sleep(0.02)
    await ob.stop(1.0)
    assert [b for _, b in send.calls] == ["x", "y"]


async def test_worker_survives_sender_exceptions(store, clock):
    calls = []

    async def flaky(to, body):
        calls.append(body)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return SENT

    ob = box(store, clock, flaky)
    ob.start()
    ob.enqueue(A, "1", ["x"])
    await asyncio.sleep(0.2)
    await ob.stop(1.0)
    # The exception left the row 'sending' (claimed). It must not be silently resent.
    assert len(calls) == 1
    assert store.recover_sending() == 1
