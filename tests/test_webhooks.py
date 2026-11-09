import asyncio
import logging
import random

import pytest
import aiohttp

from hermes_ussd import webhooks
from hermes_ussd.outbox import Outbox
from hermes_ussd.service import Service
from hermes_ussd.settings import load_settings
from hermes_ussd.sms.client import Outcome

from conftest import SECRET

A = "+254700000001"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
async def env(store, clock):
    settings = load_settings({"AT_USERNAME": "u", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET,
                              "AT_ALLOWED_USERS": A}.get)
    submitted = []

    async def send(to, body):
        return Outcome("sent", message_id="m")

    svc = Service(store, Outbox(store, send, settings, clock=clock), settings, lambda *a: submitted.append(a))
    runner, port = await webhooks.serve(webhooks.make_app(svc, SECRET), "127.0.0.1", 0)
    client = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}")
    yield client, svc, submitted
    await client.close()
    await runner.cleanup()


def body(**kw):
    base = {"sessionId": "S1", "phoneNumber": A, "text": "", "serviceCode": "*384*1#", "networkCode": "63902"}
    base.update(kw)
    return base


async def test_valid_ussd_hop(env):
    client, _, _ = env
    r = await client.post(f"/{SECRET}/ussd", data=body())
    assert r.status == 200 and r.content_type == "text/plain"
    assert (await r.text()).startswith("CON Hermes")


async def test_ask_over_http_reaches_the_agent_queue(env):
    client, _, sub = env
    r = await client.post(f"/{SECRET}/ussd", data=body(text="1*hello there"))
    assert (await r.text()).startswith("END Got it") and sub[0][1] == "hello there"


async def test_wrong_secret_is_bare_404(env):
    client, _, sub = env
    r = await client.post(f"/{SECRET[:-1]}x/ussd", data=body(text="1*pwn"))
    assert r.status == 404 and await r.text() == "" and sub == []


async def test_every_rejection_is_byte_identical(env):
    """A scanner must not be able to tell a wrong secret from a wrong method or a made-up path."""
    client, _, _ = env
    rejections = [
        await client.post("/nope/ussd", data=body()),
        await client.get(f"/{SECRET}/ussd"),
        await client.put(f"/{SECRET}/ussd", data=body()),
        await client.post(f"/{SECRET}/ussd", json={"a": 1}),
        await client.post(f"/{SECRET}/ussd", data={"sessionId": "x"}),
        await client.post("/", data=body()),
        await client.post("/a/b/c/d", data=body()),
        await client.get("/health"),
    ]
    shapes = {(r.status, await r.text(), r.content_type) for r in rejections}
    assert shapes == {(404, "", "text/plain")}


async def test_oversized_body_is_rejected(env):
    client, _, sub = env
    r = await client.post(f"/{SECRET}/ussd", data=body(text="1*" + "a" * 10_000))
    assert r.status == 404 and sub == []


async def test_declared_content_length_over_limit_is_rejected(env):
    client, _, _ = env
    r = await client.post(f"/{SECRET}/ussd", data=b"x=" + b"a" * 5000, headers=FORM)
    assert r.status == 404


async def test_non_form_content_types_rejected(env):
    client, _, _ = env
    for ctype in ("application/json", "text/plain", "multipart/form-data; boundary=x", ""):
        r = await client.post(f"/{SECRET}/ussd", data=b"sessionId=1&phoneNumber=%2B254700000001",
                              headers={"Content-Type": ctype} if ctype else {})
        assert r.status == 404, ctype


async def test_non_utf8_and_junk_bodies_never_5xx(env):
    client, _, _ = env
    rng = random.Random(7)
    payloads = [b"\xff\xfe\x00", b"%", b"%zz=1", b"&&&&", b"a=" + b"%ff" * 50,
                b"&".join(b"k%d=v" % i for i in range(200))]
    payloads += [bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300))) for _ in range(40)]
    for p in payloads:
        r = await client.post(f"/{SECRET}/ussd", data=p, headers=FORM)
        assert r.status in (200, 404), p
    for p in payloads:
        r = await client.post(f"/{SECRET}/sms", data=p, headers=FORM)
        assert r.status in (200, 404), p


async def test_non_ascii_secret_candidate_does_not_crash(env):
    client, _, _ = env
    r = await client.post("/%C3%A9%F0%9F%98%80/ussd", data=body())
    assert r.status == 404


def test_secret_comparison_is_constant_time_function():
    import inspect

    assert "compare_digest" in inspect.getsource(webhooks.secret_matches)
    assert webhooks.secret_matches(SECRET, SECRET) and not webhooks.secret_matches("é", SECRET)


async def test_slow_service_gets_end_before_gateway_timeout(env, monkeypatch):
    client, svc, _ = env
    monkeypatch.setattr(webhooks, "USSD_DEADLINE", 0.1)

    async def slow(form):
        await asyncio.sleep(5)

    monkeypatch.setattr(svc, "ussd", slow)
    r = await client.post(f"/{SECRET}/ussd", data=body())
    assert r.status == 200 and (await r.text()).startswith("END Service busy")


async def test_crashing_service_gets_end(env, monkeypatch):
    client, svc, _ = env

    async def boom(form):
        raise RuntimeError("x")

    monkeypatch.setattr(svc, "ussd", boom)
    assert (await (await client.post(f"/{SECRET}/ussd", data=body())).text()) == "END Sorry, try again."


async def test_invalid_screen_from_service_is_replaced(env, monkeypatch):
    client, svc, _ = env

    async def bad(form):
        return "x" * 500

    monkeypatch.setattr(svc, "ussd", bad)
    assert (await (await client.post(f"/{SECRET}/ussd", data=body())).text()) == "END Sorry, try again."


async def test_sms_and_dlr_endpoints(env):
    client, svc, sub = env
    r = await client.post(f"/{SECRET}/sms", data={"from": A, "to": "1", "text": "hi", "id": "i1"})
    assert r.status == 200 and await r.text() == "" and len(sub) == 1
    r = await client.post(f"/{SECRET}/dlr", data={"id": "x", "status": "Success"})
    assert r.status == 200


async def test_sms_handler_crash_still_acks(env, monkeypatch):
    client, svc, _ = env

    async def boom(form):
        raise RuntimeError("x")

    monkeypatch.setattr(svc, "sms_in", boom)
    assert (await client.post(f"/{SECRET}/sms", data={"from": A, "text": "x"})).status == 200


async def test_secret_never_appears_in_logs(env, caplog):
    client, svc, _ = env
    caplog.set_level(logging.DEBUG)
    await client.post(f"/{SECRET}/ussd", data=body(text="1*hello"))
    await client.post(f"/{SECRET}x/ussd", data=body())
    await client.post(f"/{SECRET}/ussd", data=b"\xff", headers=FORM)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in text and A not in text and "hello" not in text


async def test_many_concurrent_hops(env):
    client, _, sub = env
    rs = await asyncio.gather(*(client.post(f"/{SECRET}/ussd", data=body(sessionId=f"S{i % 5}")) for i in range(30)))
    assert all(r.status == 200 for r in rs)
