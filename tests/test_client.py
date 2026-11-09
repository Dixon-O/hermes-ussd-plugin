import json

import httpx
import pytest

from hermes_ussd.sms.client import ATClient, Outcome, classify_exception, parse_response

from fake_at import FakeAT

LOOPBACK = frozenset({"127.0.0.1"})


def body(code, **extra):
    rec = {"statusCode": code, "number": "+254700000001", "status": "x", "cost": "KES 0.8", "messageId": "ATXid_1"}
    rec.update(extra)
    return json.dumps({"SMSMessageData": {"Message": "ok", "Recipients": [rec]}})


@pytest.mark.parametrize("status,payload,kind", [
    (201, body(100), "sent"), (201, body(101), "sent"), (201, body(102), "sent"),
    (201, body(405), "balance"),
    (201, body(406), "permanent"), (201, body(403), "permanent"), (201, body(402), "permanent"),
    (201, body(407), "retry"), (201, body(500), "retry"), (201, body(502), "retry"),
    (201, body(999), "permanent"),
    (201, body("abc"), "unknown"),
    (201, "<html>", "unknown"),
    (201, json.dumps({"SMSMessageData": {"Message": "nope", "Recipients": []}}), "permanent"),
    (201, json.dumps({}), "permanent"),
    (401, "bad", "auth"), (403, "bad", "auth"),
    (400, "bad", "permanent"), (404, "x", "permanent"),
    (429, "slow down", "retry"), (500, "x", "retry"), (503, "x", "retry"),
])
def test_parse_response(status, payload, kind):
    assert parse_response(status, payload).kind == kind


def test_parse_sent_extracts_id_and_cost():
    o = parse_response(201, body(101))
    assert o.message_id == "ATXid_1" and o.cost == "KES 0.8" and o.code == 101


def test_exception_classification():
    assert classify_exception(httpx.ConnectError("x")).kind == "retry"
    assert classify_exception(httpx.ConnectTimeout("x")).kind == "retry"
    assert classify_exception(httpx.ReadTimeout("x")).kind == "unknown"
    assert classify_exception(httpx.RemoteProtocolError("x")).kind == "unknown"
    assert classify_exception(RuntimeError("x")).kind == "unknown"


def test_host_allowlist_enforced():
    with pytest.raises(ValueError):
        ATClient("u", "k", base_url="https://evil.example.com/version1/messaging")
    ATClient("u", "k", sandbox=True, http=object())
    ATClient("u", "k", sandbox=False, http=object())


async def make(fake: FakeAT, **kw) -> ATClient:
    return ATClient("sandbox", fake.api_key, base_url=fake.url, allow_hosts=LOOPBACK,
                    http=httpx.AsyncClient(), **kw)


async def test_send_success_roundtrip():
    fake = await FakeAT().start()
    try:
        c = await make(fake, sender="HERMES")
        o = await c.send_sms("+254700000001", "hello")
        assert o.kind == "sent" and o.message_id
        assert fake.sent == [("+254700000001", "hello")]
    finally:
        await fake.stop()


async def test_wrong_key_is_auth():
    fake = await FakeAT().start()
    try:
        c = ATClient("sandbox", "wrong", base_url=fake.url, allow_hosts=LOOPBACK, http=httpx.AsyncClient())
        o = await c.send_sms("+254700000001", "hi")
        assert o.kind == "auth" and "wrong" not in o.detail
    finally:
        await fake.stop()


async def test_redirect_is_not_followed():
    fake = await FakeAT(fail_rate=1.0, fail_mode="redirect").start()
    try:
        o = await (await make(fake)).send_sms("+254700000001", "hi")
        assert o.kind == "permanent" and "redirect" in o.detail
    finally:
        await fake.stop()


async def test_connection_refused_is_retry():
    fake = await FakeAT().start()
    url = fake.url
    await fake.stop()
    c = ATClient("sandbox", "k", base_url=url, allow_hosts=LOOPBACK, http=httpx.AsyncClient())
    assert (await c.send_sms("+254700000001", "hi")).kind == "retry"


async def test_dropped_connection_after_processing_is_unknown_never_retry():
    fake = await FakeAT(fail_rate=1.0, fail_mode="drop").start()
    try:
        o = await (await make(fake)).send_sms("+254700000001", "hi")
        assert o.kind == "unknown"
        assert len(fake.sent) == 1  # the server did process it, which is why resending would duplicate
    finally:
        await fake.stop()


async def test_read_timeout_is_unknown():
    fake = await FakeAT(fail_rate=1.0, fail_mode="slow").start()
    try:
        c = await make(fake, timeout=0.2)
        assert (await c.send_sms("+254700000001", "hi")).kind == "unknown"
    finally:
        await fake.stop()


async def test_probe_auth():
    fake = await FakeAT().start()
    try:
        assert (await (await make(fake)).probe_auth()).kind == "ok"
        bad = ATClient("sandbox", "nope", base_url=fake.url, allow_hosts=LOOPBACK, http=httpx.AsyncClient())
        assert (await bad.probe_auth()).kind == "auth"
        assert fake.sent == []  # probing never sends
    finally:
        await fake.stop()


async def test_factory_client_is_created_lazily_and_closed():
    made = []

    def factory():
        c = httpx.AsyncClient()
        made.append(c)
        return c

    fake = await FakeAT().start()
    try:
        c = ATClient("sandbox", fake.api_key, base_url=fake.url, allow_hosts=LOOPBACK, http_factory=factory)
        assert not made
        await c.send_sms("+254700000001", "x")
        assert len(made) == 1
        await c.aclose()
        assert made[0].is_closed
    finally:
        await fake.stop()


def test_outcome_is_value_object():
    assert Outcome("sent") == Outcome("sent")
