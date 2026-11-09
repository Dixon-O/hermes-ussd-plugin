"""Security properties end to end (the threat table in SECURITY.md maps to these tests)."""

from __future__ import annotations

import asyncio
import logging
import os
import stat

import pytest

pytest.importorskip("gateway.platforms.base")

from hermes_ussd import adapter as adapter_mod  # noqa: E402
from hermes_ussd.settings import load_settings  # noqa: E402

from conftest import SECRET  # noqa: E402
from test_hermes_integration import A, STRANGER, hermes_env, hop, running, wait_for  # noqa: E402,F401

B = "+254700000002"
API_KEY = "k" * 20


async def test_no_secret_key_number_or_text_in_logs_across_a_full_flow(running, caplog):
    """T-logs: the webhook secret, API key, full phone numbers and message text never reach a line logged
    by this plugin (or by aiohttp / httpx beneath it), even at DEBUG, through ask -> agent -> SMS ->
    readback -> stranger -> STOP. Hermes core itself logs the chat id (the phone number) when it sends a
    reply, exactly as it does for its Signal/SMS/WhatsApp adapters; that is documented in SECURITY.md."""
    a, fake, http, seen = running
    caplog.set_level(logging.DEBUG)
    text = "my confidential medical question"
    await http.post(f"/{SECRET}/ussd", data=hop(f"1*{text}"))
    assert await wait_for(lambda: fake.sent)
    await http.post(f"/{SECRET}/ussd", data=hop("2", session="S2"))
    await http.post(f"/{SECRET}/ussd", data=hop("1*hi", number=STRANGER, session="S3"))
    await http.post(f"/{SECRET}/sms", data={"from": A, "to": "1", "text": "STOP", "id": "stop1"})
    await http.post("/wrong-secret/ussd", data=hop())
    await asyncio.sleep(0.3)
    ours = [r for r in caplog.records if r.name.split(".")[0] in ("hermes_ussd", "aiohttp", "httpx", "httpcore")]
    assert ours, "expected the plugin to log something"
    logs = "\n".join(r.getMessage() + " " + str(r.args or "") for r in ours)
    for forbidden in (SECRET, API_KEY, A, STRANGER, text, "Echo:"):
        assert forbidden not in logs, f"{forbidden!r} leaked into logs"
    everything = "\n".join(r.getMessage() + " " + str(r.args or "") for r in caplog.records)
    for forbidden in (SECRET, API_KEY, text):  # no logger anywhere may carry these
        assert forbidden not in everything


def test_settings_repr_and_str_hide_credentials():
    s = load_settings({"AT_USERNAME": "sandbox", "AT_API_KEY": API_KEY, "AT_WEBHOOK_SECRET": SECRET}.get)
    assert API_KEY not in repr(s) and SECRET not in repr(s)
    assert API_KEY not in str(s) and SECRET not in str(s)


def test_database_is_private_to_the_owner(hermes_env):
    store = adapter_mod.open_store()
    try:
        path = store.conn.execute("PRAGMA database_list").fetchone()[2]
        assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0, "database readable by other users"
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) & 0o077 == 0
    finally:
        store.close()


async def test_one_number_can_never_read_another_numbers_answer(running):
    a, fake, http, seen = running
    a._store.put_reply("R1", A, "A's private answer", False, 1)
    r = await http.post(f"/{SECRET}/ussd", data=hop("2", number=B, session="X"))  # B is not even allowlisted
    assert "private answer" not in await r.text()
    # forging the number inside a legitimate session cannot cross over either: identity is the number
    r = await http.post(f"/{SECRET}/ussd", data=hop("2", number=A, session="Y"))
    assert "A's private answer" in await r.text()


@pytest.mark.parametrize("payload", [
    "1*x'); DROP TABLE asks;--",
    "1*" + "' OR '1'='1",
    "1*\x00\x01\x02",
    "1*‮⁦spoof",
    "1*" + "A" * 3000,
    "*" * 500,
    "1*\n\r\n\rCON injected",
    "98*98*98*98*98",
    "0*0*0*00*00",
])
async def test_hostile_ussd_text_never_breaks_the_service(running, payload):
    a, fake, http, seen = running
    r = await http.post(f"/{SECRET}/ussd", data=hop(payload, session="H"))
    screen = await r.text()
    assert r.status == 200 and screen.split(" ", 1)[0] in ("CON", "END") and len(screen) <= 182
    # the schema survived
    assert a._store.conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0] >= 0
    # whatever the agent received is bounded and carries no gateway-command authority
    for ev in seen:
        assert len(ev.text) <= 200 and ev.allow_gateway_control is False


async def test_slash_commands_over_sms_are_inert_by_default(running):
    """T-cmd: with AT_ALLOW_COMMANDS unset an SMS like '/new' or '/sethome' is plain text, never a command."""
    a, fake, http, seen = running
    await http.post(f"/{SECRET}/sms", data={"from": A, "to": "1", "text": "/sethome", "id": "c1"})
    assert await wait_for(lambda: seen)
    assert seen[0].allow_gateway_control is False


async def test_body_over_the_limit_is_a_bare_404(running):
    a, fake, http, seen = running
    r = await http.post(f"/{SECRET}/ussd", data=hop("1*" + "A" * 5000, session="big"))
    assert r.status == 404 and await r.text() == "" and seen == []


async def test_stranger_probing_leaks_nothing_about_the_allowlist(running):
    a, fake, http, seen = running
    screens = set()
    for i, num in enumerate([STRANGER, B, "+254711000000", "+254722222222"]):
        r = await http.post(f"/{SECRET}/ussd", data=hop("", number=num, session=f"P{i}"))
        screens.add(await r.text())
    assert screens == {"END This service is private."}  # identical for every non-allowlisted number
    r = await http.post(f"/{SECRET}/ussd", data=hop("", number="garbage", session="Pg"))
    assert await r.text() == "END Sorry, try again."  # malformed input only; says nothing about the allowlist
    r = await http.post(f"/{SECRET}/ussd", data=hop("", number="", session="Pe"))
    assert r.status == 404 and await r.text() == ""


async def test_stop_is_honoured_immediately_and_survives_purge(running):
    a, fake, http, seen = running
    await http.post(f"/{SECRET}/sms", data={"from": A, "to": "1", "text": "stop", "id": "s1"})
    await asyncio.sleep(0.2)
    assert a._store.is_opted_out(A)
    a._store.purge(0)
    assert a._store.is_opted_out(A), "retention purge must never erase an opt-out"
    await a.send(A, "should not be texted")
    await asyncio.sleep(0.3)
    assert fake.sent == []


async def test_replaying_an_old_sms_id_cannot_trigger_another_turn(running):
    a, fake, http, seen = running
    form = {"from": A, "to": "1", "text": "once only", "id": "dup-1"}
    for _ in range(5):
        await http.post(f"/{SECRET}/sms", data=form)
    assert await wait_for(lambda: seen)
    await asyncio.sleep(0.3)
    assert len(seen) == 1


def test_no_agent_callable_surface_is_registered():
    """T-tools: a prompt-injected agent must have no tool that sends SMS or reads the store."""
    import hermes_ussd
    from fakectx import FakeCtx

    class Strict(FakeCtx):
        def __getattr__(self, name):
            if name.startswith("register_") and name not in (
                    "register_platform", "register_cli_command", "register_command", "register_skill"):
                raise AssertionError(f"unexpected ctx.{name}")
            raise AttributeError(name)

    hermes_ussd.register(Strict())


async def test_phone_users_get_a_restricted_toolset(running):
    a, *_ = running
    assert a.toolsets_for_source(None) == ["safe"]


async def test_second_gateway_on_the_same_account_and_port_is_refused(running):
    """T-lock: two gateways (e.g. two profiles) cannot serve the same Africa's Talking app and port."""
    from gateway.config import PlatformConfig

    a, *_ = running
    second = adapter_mod.ATAdapter(PlatformConfig(enabled=True))
    try:
        assert await second.connect() is False
        assert second.has_fatal_error
    finally:
        await second.disconnect()
