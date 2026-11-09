"""Chaos and load: the delivery guarantees hold under failures, duplicates and concurrency.

Runs the real adapter, webhook listener, store and outbox against a fault-injecting fake Africa's
Talking. Guarantees under test:
  * zero lost replies: every accepted ask has its answer stored (readable over USSD) and an outbox row
    that ends in a recorded terminal status (nothing stuck queued/sending);
  * zero duplicate SMS, even when the callback or the network misbehaves;
  * ambiguous failures are never resent.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("gateway.platforms.base")

from hermes_ussd import outbox as outbox_mod  # noqa: E402

from conftest import SECRET  # noqa: E402
from test_hermes_integration import hermes_env, hop, running, wait_for  # noqa: E402,F401

N = 1000


def numbers(n: int) -> list[str]:
    return [f"+254700{i:06d}" for i in range(1, n + 1)]


@pytest.fixture
def chaos_env(hermes_env, monkeypatch):
    """1,000 allowlisted numbers, generous limits, fast retries (same shape, shorter waits)."""
    monkeypatch.setenv("AT_ALLOWED_USERS", ",".join(numbers(N)))
    monkeypatch.setenv("AT_ASKS_PER_HOUR", "1000")
    monkeypatch.setenv("AT_HOPS_PER_MINUTE", "1000")
    monkeypatch.setenv("AT_DAILY_SMS_CAP", "100000")
    monkeypatch.setattr(outbox_mod, "RETRY_DELAYS", (0.01, 0.02, 0.03, 0.05, 0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(outbox_mod, "POLL", 0.05)


def statuses(a) -> dict:
    return {r["status"]: r["n"] for r in a._store.conn.execute(
        "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")}


def stored_replies(a) -> int:
    return a._store.conn.execute("SELECT COUNT(*) AS n FROM replies").fetchone()["n"]


async def fire(http, nums, *, dup_rate=0.0, seed=7):
    """Dial every number once, in concurrent waves; optionally replay some callbacks verbatim."""
    import random

    rng = random.Random(seed)
    screens: list[str] = []

    async def one(i, num):
        form = hop(f"1*question {i}", number=num, session=f"S{i}")
        r = await http.post(f"/{SECRET}/ussd", data=form)
        screens.append(await r.text())
        if rng.random() < dup_rate:  # the gateway retried the same callback
            await http.post(f"/{SECRET}/ussd", data=form)

    for start in range(0, len(nums), 50):
        await asyncio.gather(*(one(start + j, n) for j, n in enumerate(nums[start:start + 50])))
    return screens


async def settle(a, timeout=90.0) -> bool:
    return await wait_for(lambda: not (set(statuses(a)) & {"queued", "sending"}) and stored_replies(a) > 0,
                          timeout)


async def test_1000_turns_with_20_percent_api_failures_lose_nothing(chaos_env, running):
    a, fake, http, seen = running
    fake.fail_rate, fake.fail_mode = 0.2, "500"
    screens = await fire(http, numbers(N))
    assert all(s.startswith("END Got it") for s in screens) and len(screens) == N
    assert await wait_for(lambda: stored_replies(a) == N, 60), "an answer was lost"
    assert await settle(a), f"outbox did not drain: {statuses(a)}"
    assert len(seen) == N
    assert statuses(a) == {"sent": N}, statuses(a)  # every reply delivered despite 1 in 5 requests failing
    assert fake.duplicates() == 0 and len(fake.sent) == N
    assert fake.requests > N  # failures really happened and were retried


async def test_duplicate_callbacks_never_cause_duplicate_sms(chaos_env, running):
    a, fake, http, seen = running
    await fire(http, numbers(300), dup_rate=0.10)
    assert await wait_for(lambda: stored_replies(a) == 300, 30)
    assert await settle(a)
    assert len(seen) == 300, "a replayed callback started a second agent turn"
    assert fake.duplicates() == 0 and len(fake.sent) == 300
    assert statuses(a) == {"sent": 300}


async def test_ambiguous_network_failures_are_never_resent(chaos_env, running):
    """'drop' = Africa's Talking processed the message but the connection died before we saw the reply.
    The right answer is 'unknown' and no retry: a resend could text the person twice."""
    a, fake, http, seen = running
    fake.fail_rate, fake.fail_mode = 0.1, "drop"
    await fire(http, numbers(300))
    assert await wait_for(lambda: stored_replies(a) == 300, 30)
    assert await settle(a)
    s = statuses(a)
    assert set(s) <= {"sent", "unknown"} and s["sent"] + s.get("unknown", 0) == 300
    assert s.get("unknown", 0) > 0, "fault injection did not trigger"
    assert fake.duplicates() == 0 and len(fake.sent) == 300  # each message delivered exactly once
    # an 'unknown' answer is still readable over USSD
    unknown_chat = a._store.conn.execute("SELECT chat_id FROM outbox WHERE status='unknown' LIMIT 1").fetchone()[0]
    r = await http.post(f"/{SECRET}/ussd", data=hop("2", number=unknown_chat, session="readback"))
    assert "Echo: question" in await r.text()


async def test_total_outage_then_recovery_delivers_everything_once(chaos_env, running):
    a, fake, http, seen = running
    fake.fail_rate, fake.fail_mode = 1.0, "500"
    await fire(http, numbers(100))
    assert await wait_for(lambda: stored_replies(a) == 100, 30)
    await asyncio.sleep(0.5)
    assert fake.sent == []  # nothing got through, nothing was lost
    # all answers are readable during the outage
    r = await http.post(f"/{SECRET}/ussd", data=hop("2", number=numbers(100)[0], session="outage"))
    assert "Echo: question" in await r.text()
    fake.fail_rate = 0.0
    # rows that exhausted retries are terminal (failed_transient) by design; none may be stuck
    assert await settle(a)
    s = statuses(a)
    assert set(s) <= {"sent", "failed_transient"}
    assert fake.duplicates() == 0


async def test_provider_balance_exhaustion_pauses_instead_of_hammering(chaos_env, running):
    a, fake, http, seen = running
    fake.fail_rate, fake.fail_mode = 1.0, "balance"
    await fire(http, numbers(60))
    assert await wait_for(lambda: stored_replies(a) == 60, 30)
    await asyncio.sleep(0.5)
    assert a._outbox.health == "balance"
    assert fake.requests <= 5, f"kept calling a provider that said the balance is empty ({fake.requests})"
    assert set(statuses(a)) == {"queued"}  # held for later, not dropped


async def test_hostile_traffic_does_not_disturb_real_users(chaos_env, running):
    """Strangers, garbage and wrong secrets hammering the listener while real users are served."""
    a, fake, http, seen = running

    async def hostile():
        for i in range(300):
            await http.post(f"/{SECRET}/ussd", data={"phoneNumber": f"+25479{i:07d}", "sessionId": f"x{i}",
                                                     "text": "1*spam"})
            await http.post(f"/wrong-secret-{i}/ussd", data={"phoneNumber": "+254700000001"})
            await http.post(f"/{SECRET}/ussd", data="\x00\xff" * 50, headers={"Content-Type": "text/plain"})

    await asyncio.gather(hostile(), fire(http, numbers(200)))
    assert await wait_for(lambda: stored_replies(a) == 200, 30)
    assert await settle(a)
    assert len(seen) == 200  # strangers never reached the agent
    assert len(fake.sent) == 200 and fake.duplicates() == 0
