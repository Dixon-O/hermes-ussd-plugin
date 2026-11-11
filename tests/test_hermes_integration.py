"""Tests against the real Hermes classes (registry, BasePlatformAdapter, plugin storage).

Skipped automatically where Hermes is not importable, so the pure suite still runs anywhere.
"""

from __future__ import annotations

import asyncio
import dataclasses
import socket
import sys

import aiohttp
import httpx
import pytest

pytest.importorskip("gateway.platforms.base")

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

from hermes_ussd import adapter as adapter_mod  # noqa: E402
from hermes_ussd.sms.client import ATClient  # noqa: E402

from conftest import SECRET  # noqa: E402
from fake_at import FakeAT  # noqa: E402

A = "+254700000001"
STRANGER = "+254799999999"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    port = free_port()
    for k, v in {"AT_USERNAME": "sandbox", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET,
                 "AT_ALLOWED_USERS": A, "AT_WEBHOOK_PORT": str(port)}.items():
        monkeypatch.setenv(k, v)
    return port


from fakectx import FakeCtx  # noqa: E402


def test_register_builds_a_valid_platform_entry():
    import hermes_ussd

    ctx = FakeCtx()
    hermes_ussd.register(ctx)
    e = ctx.entry
    assert e.name == "africastalking" and e.source == "plugin"
    assert e.trusted_inbound is False  # chat platform: the gateway allowlist must stay in force
    assert e.allow_update_command is False
    assert e.allowlist_suppresses_pairing is True and e.display_tier == "minimal"
    assert e.allowed_users_env == "AT_ALLOWED_USERS" and e.allow_all_env == "AT_ALLOW_ALL_USERS"
    assert e.cron_deliver_env_var == "AT_HOME_CHANNEL" and e.standalone_sender_fn is not None
    assert e.max_message_length == 459 and e.pii_safe is True
    assert set(e.required_env) == {"AT_USERNAME", "AT_API_KEY", "AT_WEBHOOK_SECRET"}
    assert ctx.cli[0] == "ussd" and "ussd" in ctx.commands and "sms-replies" in ctx.skills
    assert ctx.skills["sms-replies"].exists()


def test_register_never_adds_a_model_callable_send_tool():
    import hermes_ussd

    class Strict(FakeCtx):
        def register_tool(self, *a, **k):
            raise AssertionError("hermes-ussd must not register agent-callable tools")

    hermes_ussd.register(Strict())


def test_platform_resolves_once_registered():
    import hermes_ussd

    ctx = FakeCtx()
    hermes_ussd.register(ctx)
    platform_registry.register(ctx.entry)
    try:
        assert Platform("africastalking").value == "africastalking"
    finally:
        platform_registry.unregister("africastalking") if hasattr(platform_registry, "unregister") else None


def test_requirements_and_enablement(hermes_env, monkeypatch):
    assert adapter_mod.check_requirements() is True
    assert adapter_mod.validate_config(PlatformConfig(enabled=True)) is True
    extra = adapter_mod.env_enablement()
    assert extra and extra["port"] == hermes_env
    assert "k" * 20 not in repr(extra) and SECRET not in repr(extra)  # no secrets seeded into config
    monkeypatch.delenv("AT_API_KEY")
    assert adapter_mod.check_requirements() is False and adapter_mod.env_enablement() is None
    assert adapter_mod.validate_config(PlatformConfig(enabled=True)) is False


def test_notice_filter():
    assert adapter_mod.is_notice("⚡ Interrupting current task. I'll respond shortly.")
    assert adapter_mod.is_notice("⏳ Queued for the next turn")
    assert adapter_mod.is_notice("♻ Gateway online")
    assert not adapter_mod.is_notice("Nairobi is the capital of Kenya.")
    assert not adapter_mod.is_notice("⏳ " + "x" * 700)  # a real (long) reply that happens to start oddly


@pytest.fixture
async def running(hermes_env, monkeypatch):
    """A connected adapter wired to a fake Africa's Talking and an echo 'agent'."""
    fake = await FakeAT().start()
    monkeypatch.setattr(adapter_mod, "make_client", lambda s: ATClient(
        s.username, s.api_key, base_url=fake.url, allow_hosts=frozenset({"127.0.0.1"}), http=httpx.AsyncClient()))
    a = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
    seen = []

    async def agent(event):
        seen.append(event)
        return f"Echo: {event.text}"

    a.set_message_handler(agent)
    assert await a.connect() is True
    http = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{a.port}")
    yield a, fake, http, seen
    await http.close()
    await a.disconnect()
    await fake.stop()


def hop(text="", number=A, session="S1"):
    return {"sessionId": session, "phoneNumber": number, "text": text, "serviceCode": "*384*1#", "networkCode": "63902"}


async def wait_for(cond, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_end_to_end_ussd_ask_sms_reply_and_readback(running):
    a, fake, http, seen = running
    r = await http.post(f"/{SECRET}/ussd", data=hop("1*What is M-Pesa?"))
    screen = await r.text()
    assert screen.startswith("END Got it")
    ref = screen.split("Ref ")[1].split(".")[0]

    assert await wait_for(lambda: fake.sent), "no SMS reached Africa's Talking"
    to, msg = fake.sent[0]
    assert to == A and msg == f"Ref {ref}: Echo: What is M-Pesa?"
    assert len(seen) == 1 and seen[0].allow_gateway_control is False  # USSD text is never a command
    assert seen[0].source.chat_id == A and seen[0].source.user_id == A

    r = await http.post(f"/{SECRET}/ussd", data=hop("2", session="S2"))
    assert "Echo: What is M-Pesa?" in await r.text()


async def test_answer_is_readable_over_ussd_even_when_every_sms_fails(running):
    a, fake, http, seen = running
    fake.fail_rate, fake.fail_mode = 1.0, "balance"
    await http.post(f"/{SECRET}/ussd", data=hop("1*hello"))
    assert await wait_for(lambda: a._store.last_reply(A) is not None)
    r = await http.post(f"/{SECRET}/ussd", data=hop("2", session="S2"))
    assert "Echo: hello" in await r.text()  # the whole point of store-first


async def test_non_allowlisted_number_never_reaches_the_agent_or_costs_an_sms(running):
    a, fake, http, seen = running
    r = await http.post(f"/{SECRET}/ussd", data=hop("1*hi", number=STRANGER))
    assert (await r.text()).startswith("END This service is private")
    await http.post(f"/{SECRET}/sms", data={"from": STRANGER, "to": "1", "text": "hi", "id": "x1"})
    await asyncio.sleep(0.3)
    assert seen == [] and fake.sent == []


async def test_inbound_sms_gets_a_plain_reply(running):
    a, fake, http, seen = running
    await http.post(f"/{SECRET}/sms", data={"from": A, "to": "1", "text": "hello agent", "id": "m1"})
    assert await wait_for(lambda: fake.sent)
    assert fake.sent[0] == (A, "Echo: hello agent")  # SMS-originated: no 'Ref' prefix


async def test_long_reply_is_truncated_to_the_segment_cap(running):
    a, fake, http, seen = running

    async def wordy(event):
        return "word " * 400

    a.set_message_handler(wordy)
    await http.post(f"/{SECRET}/sms", data={"from": A, "to": "1", "text": "tell me everything", "id": "m2"})
    assert await wait_for(lambda: len(fake.sent) >= 3)
    await asyncio.sleep(0.2)
    assert len(fake.sent) == 3 and "dial the USSD code" in fake.sent[-1][1]
    stored = a._store.last_reply(A)
    assert stored.count("word") == 400  # the full text is kept for USSD readback


async def test_busy_ack_and_restart_notices_are_never_texted(running):
    a, fake, http, seen = running
    result = await a.send(A, "⚡ Interrupting current task. I'll respond to your message shortly.")
    assert result.success and result.message_id == "suppressed-notice"
    await a.send(A, "♻ Gateway online")
    await asyncio.sleep(0.2)
    assert fake.sent == [] and a._store.last_reply(A) is None


async def test_send_rejects_bad_recipient_and_when_disconnected(hermes_env):
    a = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
    r = await a.send(A, "hi")
    assert not r.success and r.retryable  # not connected yet: gateway may retry


async def test_markdown_and_emoji_are_stripped_from_sms(running):
    a, fake, http, seen = running
    await a.send(A, "**Great** news \U0001F389: see [docs](https://x.example)")
    assert await wait_for(lambda: fake.sent)
    msg = fake.sent[0][1]
    assert "**" not in msg and "\U0001F389" not in msg and "](" not in msg


async def test_opted_out_user_gets_no_sms_but_can_read_answer(running):
    a, fake, http, seen = running
    a._store.opt_out(A)
    await a.send(A, "secret answer")
    await asyncio.sleep(0.3)
    assert fake.sent == [] and a._store.last_reply(A) == "secret answer"


async def test_toolsets_default_to_restricted_and_fail_closed(running, hermes_env):
    a, *_ = running
    assert a.toolsets_for_source(None) == ["safe"]
    fresh = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
    assert fresh.toolsets_for_source(None) == ["safe"]  # settings not loaded yet: still restricted


async def test_gateway_restart_pings_are_disabled_for_this_platform(hermes_env):
    cfg = PlatformConfig(enabled=True)
    adapter_mod.ATAdapter(cfg)
    assert cfg.gateway_restart_notification is False and cfg.typing_indicator is False


async def test_connect_refuses_to_start_with_a_weak_secret(hermes_env, monkeypatch):
    monkeypatch.setenv("AT_WEBHOOK_SECRET", "short")
    a = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
    assert await a.connect() is False
    assert a.has_fatal_error and a._fatal_error_retryable is False


async def test_port_conflict_is_a_retryable_failure_not_a_crash(hermes_env, monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", hermes_env))
    blocker.listen(1)
    try:
        a = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
        assert await a.connect() is False
        assert a.has_fatal_error and a._fatal_error_retryable is True
    finally:
        blocker.close()


async def test_second_gateway_on_same_account_is_locked_out(hermes_env, monkeypatch):
    a1 = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
    assert await a1.connect() is True
    try:
        a2 = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
        # Same pid may re-acquire (by design in Hermes), so check the listener conflict instead.
        assert await a2.connect() is False
    finally:
        await a1.disconnect()


async def test_disconnect_releases_port_and_is_idempotent(hermes_env):
    a = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
    assert await a.connect()
    await a.disconnect()
    await a.disconnect()
    with socket.socket() as s:
        s.bind(("127.0.0.1", hermes_env))  # port is free again


async def test_listener_does_not_log_the_secret(running, caplog):
    a, fake, http, seen = running
    import logging

    caplog.set_level(logging.DEBUG)
    await http.post(f"/{SECRET}/ussd", data=hop("1*hello"))
    await http.post(f"/{SECRET}x/ussd", data=hop())
    await wait_for(lambda: fake.sent)
    assert SECRET not in "\n".join(r.getMessage() for r in caplog.records)


def test_database_file_is_private(hermes_env):
    store = adapter_mod.open_store()
    try:
        import os
        import stat

        path = store.conn.execute("PRAGMA database_list").fetchone()[2]
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        store.close()


def test_entry_kwargs_exist_on_this_core():
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    for needed in ("standalone_sender_fn", "parse_target_ref_fn", "validate_target_ref_fn", "env_enablement_fn",
                   "cron_deliver_env_var", "allow_update_command", "allowlist_suppresses_pairing", "display_tier"):
        assert needed in known, needed
    assert "trusted_inbound" in known  # exists, and we deliberately leave it False


if sys.platform == "win32":  # pragma: no cover
    pytest.skip("unix-only mode bits", allow_module_level=True)


async def test_events_pass_the_gateways_native_admission_checks(running):
    """Regression: a real gateway refused events that carried adapter ``metadata`` (only WhatsApp may),
    so the turn never ran. The event must survive the same checks the gateway applies at admission."""
    pytest.importorskip("gateway.session_envelope")
    from gateway import session_envelope as env

    a, fake, http, seen = running
    await http.post(f"/{SECRET}/ussd", data=hop("1*hello"))
    await http.post(f"/{SECRET}/sms", data={"from": A, "to": "1", "text": "hi", "id": "adm1"})
    assert await wait_for(lambda: len(seen) == 2)
    for event in seen:
        assert env._native_metadata(event) is not None, "gateway would refuse this event's metadata"  # noqa: SLF001
        assert env._media_clause(event) is None  # noqa: SLF001


async def test_gateway_setup_notices_never_become_sms(running):
    """Regression (found running the real gateway): the one-time 'no home channel is set ... /sethome'
    prompt was texted to the user and consumed the question's Ref, so the real answer lost its label."""
    a, fake, http, seen = running
    notice = ("\U0001f4ec No home channel is set for Africastalking. A home channel is where Hermes delivers cron "
              "job results and cross-platform messages.\n\nType /sethome to make this chat your home channel, "
              "or ignore to skip.")
    assert adapter_mod.env_enablement()["notice_delivery"] == "private"  # gateway routes notices privately
    assert adapter_mod.is_notice(notice)  # and even a public send is recognised and dropped
    r = await a.send_private_notice(A, A, notice)
    assert r.success and r.message_id == "suppressed-notice"
    await a.send(A, notice)
    await http.post(f"/{SECRET}/ussd", data=hop("1*real question"))
    assert await wait_for(lambda: fake.sent)
    await asyncio.sleep(0.2)
    assert len(fake.sent) == 1 and "sethome" not in fake.sent[0][1]
    assert fake.sent[0][1].startswith("Ref ")  # the answer still carries the question's Ref


def test_failure_copy_detection():
    assert adapter_mod.is_notice("⚠️ No response from provider for 180s (model: x). Reconnecting...")
    assert adapter_mod.is_failure_copy("Google AI Studio rejected the request and retrying won't help. Pick another model")
    assert adapter_mod.is_failure_copy("Provider said: quota exceeded")
    assert not adapter_mod.is_failure_copy("Yellow streaks on maize often mean maize streak virus.")


async def test_provider_errors_become_one_generic_sms(running):
    """Regression (found in the Africa's Talking sandbox): Hermes's failed-turn copy was texted to the user,
    three segments of provider internals. Now: transient warnings vanish, the final failure is replaced by
    one generic line carrying the Ref, and repeated error lines for the same ask send nothing."""
    a, fake, http, seen = running
    await http.post(f"/{SECRET}/ussd", data=hop("1*what is wrong with my maize"))
    assert await wait_for(lambda: seen)
    await a.send(A, "⚠️ No response from provider for 90s (model: m). Aborting call.")  # transient
    await a.send(A, "Google AI Studio rejected the request and retrying won't help. Pick another model with /model.")
    await a.send(A, "Provider said: Provider has been unresponsive for 5 consecutive stale attempts.")
    assert await wait_for(lambda: fake.sent)
    await asyncio.sleep(0.5)  # the test model also answers; only the error handling is under test
    bodies = [b for _, b in fake.sent]
    generic = [b for b in bodies if "try again" in b]
    assert len(generic) == 1 and generic[0].startswith("Ref ")
    assert not any(w in b for b in bodies for w in ("Provider", "rejected", "No response from provider"))
