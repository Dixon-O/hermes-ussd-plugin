import sqlite3

import pytest

from hermes_ussd import store as st

A, B = "+254700000001", "+254700000002"


def test_migrate_is_idempotent_and_refuses_newer(tmp_path, clock):
    conn = sqlite3.connect(tmp_path / "x.db")
    s = st.Store(conn, clock)
    s.migrate()
    s.migrate()
    conn.execute("UPDATE schema_meta SET v='99' WHERE k='version'")
    conn.commit()
    with pytest.raises(RuntimeError, match="newer"):
        s.migrate()


def test_refs_are_four_digits_and_unique(store):
    refs = set()
    for _ in range(200):
        r = store.new_ref()
        assert len(r) == 4 and r.isdigit()
        store.add_ask(r, A, "ussd")
        refs.add(r)
    assert len(refs) == 200


def test_ask_claim_is_fifo_and_expires(store, clock):
    store.add_ask("1111", A, "ussd")
    clock.advance(1)
    store.add_ask("2222", A, "sms")
    assert store.pending_asks(A) == 2
    assert store.claim_oldest_ask(A) == ("1111", "ussd")
    assert store.claim_oldest_ask(A) == ("2222", "sms")
    assert store.claim_oldest_ask(A) is None
    store.add_ask("3333", B, "ussd")
    clock.advance(st.ASK_TTL + 1)
    assert store.claim_oldest_ask(B) is None and store.pending_asks(B) == 0


def test_ask_claims_are_per_number(store):
    store.add_ask("1111", A, "ussd")
    assert store.claim_oldest_ask(B) is None


def test_claim_inbound_replay_returns_stored_ref(store):
    assert store.claim_inbound("k1", "4821") == (True, "4821")
    assert store.claim_inbound("k1", "9999") == (False, "4821")
    assert store.claim_inbound("sms:7") == (True, None)
    assert store.claim_inbound("sms:7") == (False, None)


def test_last_reply_is_newest(store, clock):
    assert store.last_reply(A) is None
    store.put_reply("1", A, "first", False, 1)
    store.put_reply("2", A, "second", False, 1)
    store.put_reply("3", B, "other", False, 1)
    assert store.last_reply(A) == "second"


def test_enqueue_is_idempotent(store):
    ids1 = store.enqueue(A, "4821", ["one", "two"])
    ids2 = store.enqueue(A, "4821", ["one", "two"])
    assert ids1 == ids2 and store.queued_count() == 2


def test_due_respects_time_and_status(store, clock):
    [rid] = store.enqueue(A, "1", ["x"])
    assert [r.id for r in store.due()] == [rid]
    assert store.mark_sending(rid)
    assert store.due() == []
    store.finish(rid, "queued", retry_at=clock() + 10)
    assert store.due() == []
    clock.advance(11)
    assert [r.id for r in store.due()] == [rid]


def test_mark_sending_claims_once(store):
    [rid] = store.enqueue(A, "1", ["x"])
    assert store.mark_sending(rid) is True
    assert store.mark_sending(rid) is False


def test_prior_segments_gate(store):
    r1, r2 = store.enqueue(A, "1", ["a", "b"])
    row2 = [r for r in store.due() if r.id == r2][0]
    assert store.prior_segments_ok(row2) is None  # segment 1 still queued
    store.mark_sending(r1)
    store.finish(r1, "sent", message_id="m1")
    assert store.prior_segments_ok(row2) is True
    store.finish(r1, "failed_permanent")
    assert store.prior_segments_ok(row2) is False


def test_recover_sending_never_requeues(store):
    [rid] = store.enqueue(A, "1", ["x"])
    store.mark_sending(rid)
    assert store.recover_sending() == 1
    assert store.status_counts() == {"unknown": 1}
    assert store.due() == []


def test_dlr_updates_only_sent_rows(store):
    [rid] = store.enqueue(A, "1", ["x"])
    assert not store.apply_dlr("m1", "Success", None)  # not sent yet, unknown id
    store.mark_sending(rid)
    store.finish(rid, "sent", message_id="m1")
    assert not store.apply_dlr("m1", "Submitted", None)  # in-flight statuses change nothing
    assert store.apply_dlr("m1", "Success", None)
    assert store.status_counts() == {"delivered": 1}
    assert not store.apply_dlr("nope", "Failed", "x")


def test_dlr_failure_recorded(store):
    [rid] = store.enqueue(A, "1", ["x"])
    store.mark_sending(rid)
    store.finish(rid, "sent", message_id="m1")
    assert store.apply_dlr("m1", "Failed", "UserInBlacklist")
    assert store.status_counts() == {"failed_delivery": 1}


def test_optout_and_prefs_roundtrip(store):
    assert not store.is_opted_out(A)
    store.opt_out(A)
    assert store.is_opted_out(A) and not store.is_opted_out(B)
    store.opt_in(A)
    assert not store.is_opted_out(A)
    assert store.get_lang(A) is None
    store.set_lang(A, "sw")
    store.set_lang(A, "en")
    assert store.get_lang(A) == "en"


def test_counters(store):
    assert store.counter("sent:x") == 0
    assert store.incr("sent:x") == 1 and store.incr("sent:x", 4) == 5


def test_day_key_uses_operator_timezone():
    # 22:00 UTC is already the next day in UTC+3.
    t = 1_800_000_000 - (1_800_000_000 % 86400) + 22 * 3600
    assert st.day_key(t, 3) != st.day_key(t, 0)


def test_purge_removes_old_keeps_live_and_optouts(store, clock):
    store.opt_out(A)
    store.put_reply("1", A, "old", False, 1)
    [done] = store.enqueue(A, "1", ["x"])
    store.mark_sending(done)
    store.finish(done, "sent", message_id="m")
    [live] = store.enqueue(B, "2", ["y"])
    store.claim_inbound("k", "1")
    clock.advance(10 * 86400)
    removed = store.purge(retention_days=7)
    assert removed["replies"] == 1 and removed["outbox"] == 1 and removed["inbound_seen"] == 1
    assert store.queued_count() == 1  # the still-queued row survives
    assert store.is_opted_out(A)  # compliance record is never purged
    store.put_reply("3", A, "new", False, 1)
    assert store.purge(retention_days=7)["replies"] == 0


def test_purge_runs_in_batches(store, clock):
    for i in range(25):
        store.put_reply(str(i), A, "x", False, 1)
    clock.advance(30 * 86400)
    assert store.purge(retention_days=7, batch=10)["replies"] == 25


def test_purge_number_keeps_optout_by_default(store):
    store.opt_out(A)
    store.set_lang(A, "sw")
    store.put_reply("1", A, "secret", False, 1)
    store.add_ask("1111", A, "ussd")
    store.enqueue(A, "1", ["x"])
    store.put_reply("2", B, "keep", False, 1)
    out = store.purge_number(A)
    assert out == {"replies": 1, "asks": 1, "outbox": 1, "prefs": 1}
    assert store.last_reply(A) is None and store.last_reply(B) == "keep"
    assert store.is_opted_out(A)
    assert store.purge_number(A, include_optout=True)["optouts"] == 1
