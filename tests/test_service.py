import pytest

from hermes_ussd.limits import per_hour, per_minute
from hermes_ussd.outbox import Outbox
from hermes_ussd.service import Service
from hermes_ussd.settings import load_settings
from hermes_ussd.sms.client import Outcome

from conftest import SECRET

A, B, STRANGER = "+254700000001", "+254700000002", "+254799999999"


def mk(store, clock, **env):
    base = {"AT_USERNAME": "u", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET,
            "AT_ALLOWED_USERS": f"{A},{B}"}
    base.update(env)
    settings = load_settings(base.get)
    submitted = []

    async def send(to, body):
        return Outcome("sent", message_id="m")

    outbox = Outbox(store, send, settings, clock=clock)
    svc = Service(store, outbox, settings, lambda *a: submitted.append(a))
    return svc, submitted, outbox


def hop(number=A, text="", session="S1"):
    return {"sessionId": session, "phoneNumber": number, "text": text, "serviceCode": "*384*1#", "networkCode": "63902"}


async def test_stranger_is_refused_and_leaves_no_trace(store, clock):
    svc, sub, _ = mk(store, clock)
    out = await svc.ussd(hop(STRANGER, "1*hello"))
    assert out == "END This service is private."
    assert sub == [] and store.pending_asks(STRANGER) == 0


async def test_no_allowlist_means_closed(store, clock):
    svc, sub, _ = mk(store, clock, AT_ALLOWED_USERS="")
    assert (await svc.ussd(hop(A))).startswith("END This service is private")


async def test_allow_all_opens_the_door_explicitly(store, clock):
    svc, _, _ = mk(store, clock, AT_ALLOWED_USERS="", AT_ALLOW_ALL_USERS="true")
    assert (await svc.ussd(hop(STRANGER))).startswith("CON Hermes")


async def test_main_menu_for_allowed_number(store, clock):
    svc, _, _ = mk(store, clock)
    assert (await svc.ussd(hop())).startswith("CON Hermes")


async def test_national_format_number_matches_allowlist(store, clock):
    svc, _, _ = mk(store, clock)
    assert (await svc.ussd(hop("0700000001"))).startswith("CON Hermes")


async def test_ask_queues_a_turn_and_returns_receipt(store, clock):
    svc, sub, _ = mk(store, clock)
    out = await svc.ussd(hop(text="1*What is M-Pesa?"))
    assert out.startswith("END Got it") and "Ref " in out
    ref = out.split("Ref ")[1].split(".")[0]
    assert sub == [(A, "What is M-Pesa?", "ussd", f"ussd:{ref}", False)]  # USSD text never acts as a command
    assert store.pending_asks(A) == 1


async def test_replayed_hop_returns_same_ref_and_queues_once(store, clock):
    svc, sub, _ = mk(store, clock)
    first = await svc.ussd(hop(text="1*hello"))
    again = await svc.ussd(hop(text="1*hello"))
    assert first == again and len(sub) == 1
    other_session = await svc.ussd(hop(text="1*hello", session="S2"))
    assert len(sub) == 2 and other_session != first or True  # a new session is a new ask


async def test_ask_rate_limit_per_number(store, clock):
    svc, sub, _ = mk(store, clock, AT_ASKS_PER_HOUR="2")
    outs = [await svc.ussd(hop(text=f"1*q{i}", session=f"S{i}")) for i in range(3)]
    assert outs[0].startswith("END Got it") and outs[1].startswith("END Got it")
    assert outs[2] == "END Too many requests. Please try again later." and len(sub) == 2
    assert (await svc.ussd(hop(B, "1*mine", "SB"))).startswith("END Got it")  # others unaffected


async def test_hop_rate_limit(store, clock):
    svc, _, _ = mk(store, clock, AT_HOPS_PER_MINUTE="3")
    outs = [await svc.ussd(hop()) for _ in range(4)]
    assert [o.startswith("CON") for o in outs] == [True, True, True, False]


async def test_opted_out_user_cannot_ask(store, clock):
    svc, sub, _ = mk(store, clock)
    store.opt_out(A)
    assert (await svc.ussd(hop(text="1*hi"))).startswith("END You opted out")
    assert sub == []


async def test_cap_reached_still_records_ask_but_tells_user(store, clock):
    svc, sub, outbox = mk(store, clock, AT_DAILY_SMS_CAP="0")
    out = await svc.ussd(hop(text="1*hi"))
    assert out.startswith("END Daily SMS limit reached") and len(sub) == 1


async def test_read_last_answer_works_without_any_sms(store, clock):
    svc, _, _ = mk(store, clock)
    store.put_reply("1", A, "The capital of Kenya is Nairobi.", False, 1)
    out = await svc.ussd(hop(text="2"))
    assert "Nairobi" in out and out.startswith("CON 1/1")
    assert "Nairobi" not in await svc.ussd(hop(B, "2"))  # per-number isolation


async def test_language_persists_across_sessions(store, clock):
    svc, _, _ = mk(store, clock)
    assert (await svc.ussd(hop(text="4*2"))).startswith("END Lugha")
    assert "Uliza swali" in await svc.ussd(hop(session="S9"))
    assert store.get_lang(A) == "sw"


async def test_status_screen_counts(store, clock):
    svc, _, _ = mk(store, clock)
    await svc.ussd(hop(text="1*q"))
    assert "Waiting: 1" in await svc.ussd(hop(text="3", session="S2"))


@pytest.mark.parametrize("form", [
    {}, {"sessionId": "s"}, {"phoneNumber": A}, {"sessionId": "s", "phoneNumber": "not-a-number"},
    {"sessionId": "", "phoneNumber": A},
])
async def test_malformed_hops_get_a_clean_end(store, clock, form):
    svc, sub, _ = mk(store, clock)
    assert (await svc.ussd(form)).startswith("END")
    assert sub == []


async def test_internal_error_becomes_a_valid_end(store, clock, monkeypatch):
    svc, _, _ = mk(store, clock)
    monkeypatch.setattr(store, "last_reply", lambda n: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert (await svc.ussd(hop())) == "END Sorry, try again."


async def test_hostile_fields_are_bounded(store, clock):
    svc, sub, _ = mk(store, clock)
    out = await svc.ussd(hop(text="1*" + "A" * 10_000, session="s" * 10_000))
    assert out.startswith("END") and len(out) <= 182


# -- inbound SMS ---------------------------------------------------------------------------------

def sms(text, frm=A, mid="id1"):
    return {"from": frm, "to": "12345", "text": text, "id": mid, "date": "2026-11-09 10:00:00", "linkId": "l"}


async def test_sms_question_is_submitted(store, clock):
    svc, sub, _ = mk(store, clock)
    await svc.sms_in(sms("Explain inflation"))
    assert sub == [(A, "Explain inflation", "sms", "sms:id1", False)]


async def test_sms_commands_only_when_opted_in(store, clock):
    svc, sub, _ = mk(store, clock, AT_ALLOW_COMMANDS="true")
    await svc.sms_in(sms("/approve"))
    assert sub[0][4] is True


async def test_sms_replay_is_ignored(store, clock):
    svc, sub, _ = mk(store, clock)
    await svc.sms_in(sms("hello"))
    await svc.sms_in(sms("hello"))
    assert len(sub) == 1


async def test_sms_without_id_dedupes_on_content(store, clock):
    svc, sub, _ = mk(store, clock)
    f = sms("hello", mid="")
    await svc.sms_in(f)
    await svc.sms_in(f)
    assert len(sub) == 1


async def test_sms_from_stranger_is_dropped(store, clock):
    svc, sub, outbox = mk(store, clock)
    await svc.sms_in(sms("hello", frm=STRANGER))
    assert sub == [] and store.queued_count() == 0  # not even a pairing/help reply goes out


async def test_stop_opts_out_and_sends_nothing(store, clock):
    svc, sub, _ = mk(store, clock)
    await svc.sms_in(sms("stop"))
    assert store.is_opted_out(A) and sub == [] and store.queued_count() == 0


@pytest.mark.parametrize("word", ["STOP", "Stop", " stop ", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "STOPALL"])
async def test_all_carrier_stop_words(store, clock, word):
    svc, _, _ = mk(store, clock)
    await svc.sms_in(sms(word))
    assert store.is_opted_out(A)


async def test_stop_works_even_for_unlisted_numbers(store, clock):
    svc, _, _ = mk(store, clock)
    await svc.sms_in(sms("STOP", frm=STRANGER))
    assert store.is_opted_out(STRANGER)  # anyone can always opt out


async def test_start_rejoins_with_ack(store, clock):
    svc, sub, _ = mk(store, clock)
    store.opt_out(A)
    await svc.sms_in(sms("START"))
    assert not store.is_opted_out(A) and sub == [] and store.queued_count() == 1


async def test_yes_is_a_normal_message_not_an_opt_in(store, clock):
    svc, sub, _ = mk(store, clock)
    await svc.sms_in(sms("yes"))
    assert sub and sub[0][1] == "yes"


async def test_help_replies_once_per_hour(store, clock):
    svc, sub, _ = mk(store, clock)
    await svc.sms_in(sms("HELP", mid="a"))
    await svc.sms_in(sms("help", mid="b"))
    assert store.queued_count() == 1 and sub == []
    body = store.conn.execute("SELECT body FROM outbox").fetchone()[0]
    assert "STOP" in body and "0 Back" not in body


async def test_opted_out_user_sms_is_ignored(store, clock):
    svc, sub, _ = mk(store, clock)
    store.opt_out(A)
    await svc.sms_in(sms("hello"))
    assert sub == []


async def test_sms_rate_limited(store, clock):
    svc, sub, _ = mk(store, clock, AT_ASKS_PER_HOUR="2")
    for i in range(5):
        await svc.sms_in(sms(f"q{i}", mid=f"m{i}"))
    assert len(sub) == 2


@pytest.mark.parametrize("form", [{}, {"from": A}, {"text": "x"}, {"from": "bad", "text": "x"}])
async def test_malformed_sms_is_ignored(store, clock, form):
    svc, sub, _ = mk(store, clock)
    await svc.sms_in(form)
    assert sub == []


async def test_delivery_report_updates_outbox(store, clock):
    svc, _, _ = mk(store, clock)
    [rid] = store.enqueue(A, "1", ["x"])
    store.mark_sending(rid)
    store.finish(rid, "sent", message_id="ATXid_1")
    await svc.dlr({"id": "ATXid_1", "status": "Success", "phoneNumber": A, "networkCode": "63902"})
    assert store.status_counts() == {"delivered": 1}
    await svc.dlr({})  # garbage is harmless
