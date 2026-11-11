"""CLI commands and the out-of-process sender. Hermes-dependent parts skip where it is not importable."""

from __future__ import annotations

import argparse
import io
import sqlite3

import httpx
import pytest

from hermes_ussd import cli
from hermes_ussd.settings import load_settings
from hermes_ussd.sms.client import ATClient, Outcome
from hermes_ussd.store import Store

try:  # the adapter needs Hermes's gateway package; tests that use it skip without it
    from hermes_ussd import adapter
except ImportError:
    adapter = None

from conftest import SECRET
from fake_at import FakeAT

A = "+254700000001"


class Env:
    """In-memory stand-in for the Hermes .env: the plugin only touches it via get/save helpers."""

    def __init__(self, **values):
        self.values = dict(values)
        self.saved: list[tuple[str, str]] = []

    def get(self, name):
        return self.values.get(name)

    def save(self, name, value):
        self.saved.append((name, value))
        self.values[name] = value


@pytest.fixture
def env(monkeypatch):
    e = Env(AT_USERNAME="sandbox", AT_API_KEY="k" * 20, AT_WEBHOOK_SECRET=SECRET, AT_ALLOWED_USERS=A)
    monkeypatch.setattr(cli, "_env_getter", lambda: e.get)
    monkeypatch.setattr(cli, "_save_env", e.save)
    return e


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"

    def open_store():
        s = Store(sqlite3.connect(path, check_same_thread=False))
        s.migrate()
        return s

    monkeypatch.setattr(adapter, "open_store", open_store)
    return open_store


def ns(**kw):
    base = dict(username=None, api_key=None, env="sandbox", public_url="", allow=None, ussd_code="",
                regenerate=False, number=None, include_optout=False, days=0, url="", code="*384*1#",
                path=None, sms=None)
    base.update(kw)
    return argparse.Namespace(**base)


def ok_probe(_s):
    return Outcome("ok")


# -- setup ------------------------------------------------------------------------------------

def test_setup_saves_values_and_prints_urls(env):
    env.values.pop("AT_WEBHOOK_SECRET")
    out = io.StringIO()
    code = cli.cmd_setup(ns(username="sandbox", api_key="k" * 20, public_url="https://x.example.com/",
                            allow=["0700000001"]), out, interactive=False, probe=ok_probe)
    text = out.getvalue()
    assert code == 0
    saved = dict(env.saved)
    assert len(saved["AT_WEBHOOK_SECRET"]) >= 24
    assert saved["AT_ALLOWED_USERS"] == "+254700000001"
    assert f"https://x.example.com/{saved['AT_WEBHOOK_SECRET']}/ussd" in text
    assert "SANDBOX" in text


def test_setup_keeps_existing_secret_unless_regenerate(env):
    out = io.StringIO()
    cli.cmd_setup(ns(username="sandbox", api_key="k" * 20), out, interactive=False, probe=ok_probe)
    assert dict(env.saved)["AT_WEBHOOK_SECRET"] == SECRET
    env.saved.clear()
    cli.cmd_setup(ns(username="sandbox", api_key="k" * 20, regenerate=True), io.StringIO(),
                  interactive=False, probe=ok_probe)
    assert dict(env.saved)["AT_WEBHOOK_SECRET"] != SECRET


def test_setup_refuses_bad_credentials_and_saves_nothing(env):
    out = io.StringIO()
    code = cli.cmd_setup(ns(username="sandbox", api_key="k" * 20), out, interactive=False,
                         probe=lambda s: Outcome("auth", detail="401"))
    assert code == 1 and env.saved == []
    assert "rejected" in out.getvalue()


def test_setup_unverifiable_credentials_warn_but_save(env):
    out = io.StringIO()
    code = cli.cmd_setup(ns(username="sandbox", api_key="k" * 20), out, interactive=False,
                         probe=lambda s: Outcome("retry", detail="timeout"))
    assert code == 0 and env.saved
    assert "warning" in out.getvalue()


def test_setup_never_prompts_without_a_terminal(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not prompt")

    monkeypatch.setattr(cli, "_prompt", boom)
    out = io.StringIO()
    assert cli.cmd_setup(ns(), out, interactive=False, probe=ok_probe) == 2
    assert "required when not run from a terminal" in out.getvalue()
    assert env.saved == []


@pytest.mark.parametrize("url", ["http://x.example.com", "https://localhost:8790", "https://127.0.0.1"])
def test_setup_rejects_non_public_urls(env, url):
    out = io.StringIO()
    assert cli.cmd_setup(ns(username="sandbox", api_key="k" * 20, public_url=url), out,
                         interactive=False, probe=ok_probe) == 2
    assert env.saved == []


def test_setup_reports_invalid_allowlist_numbers(env):
    out = io.StringIO()
    cli.cmd_setup(ns(username="sandbox", api_key="k" * 20, allow=["abc", "+254700000009"]), out,
                  interactive=False, probe=ok_probe)
    assert "ignored invalid" in out.getvalue()
    assert dict(env.saved)["AT_ALLOWED_USERS"] == "+254700000009"


# -- rotate ------------------------------------------------------------------------------------

def test_rotate_secret_changes_it(env):
    out = io.StringIO()
    assert cli.cmd_rotate_secret(ns(public_url="https://h.example.com"), out) == 0
    new = env.values["AT_WEBHOOK_SECRET"]
    assert new != SECRET and new in out.getvalue()
    assert SECRET not in out.getvalue()


def test_rotate_without_existing_secret_fails(env):
    env.values.pop("AT_WEBHOOK_SECRET")
    assert cli.cmd_rotate_secret(ns(), io.StringIO()) == 1


# -- doctor ------------------------------------------------------------------------------------

def by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_healthy_setup(env, db):
    s = db()
    checks = by_name(cli.run_doctor(probe=ok_probe, store=s, listening=lambda h, p: False))
    assert checks["configuration"].status == "ok"
    assert checks["Africa's Talking credentials"].status == "ok"
    assert checks["database"].status == "ok"
    assert checks["access"].status == "ok"
    assert checks["toolsets"].status == "ok"
    assert checks["listener"].status == "warn"


def test_doctor_flags_misconfiguration(env, db):
    env.values.update(AT_ALLOW_ALL_USERS="true", AT_TOOLSETS="terminal,web", AT_ENV="production")
    checks = by_name(cli.run_doctor(probe=lambda s: Outcome("auth", detail="401"), store=db(),
                                    listening=lambda h, p: False))
    assert checks["access"].status == "warn" and "EVERYONE" in checks["access"].detail
    assert checks["toolsets"].status == "fail"
    assert checks["Africa's Talking credentials"].status == "fail"
    assert checks["mode"].status == "ok"


def test_doctor_fails_cleanly_without_configuration(env):
    env.values.clear()
    checks = cli.run_doctor(probe=ok_probe)
    assert [c.status for c in checks] == ["fail"]


def test_doctor_flags_username_environment_mismatch(env, db):
    env.values["AT_USERNAME"] = "myshop"
    checks = by_name(cli.run_doctor(probe=ok_probe, store=db(), listening=lambda h, p: False))
    assert checks["username/environment"].status == "warn"
    env.values.update(AT_USERNAME="sandbox", AT_ENV="production")
    checks = by_name(cli.run_doctor(probe=ok_probe, store=db(), listening=lambda h, p: False))
    assert checks["username/environment"].status == "fail"
    env.values.update(AT_USERNAME="myshop")
    checks = by_name(cli.run_doctor(probe=ok_probe, store=db(), listening=lambda h, p: False))
    assert "username/environment" not in checks


def test_doctor_fails_on_cronjob_toolset(env, db):
    """Hermes runs cron jobs with the full toolset, so 'cronjob' for phone users defeats the safe pin."""
    env.values["AT_TOOLSETS"] = "safe,cronjob"
    checks = by_name(cli.run_doctor(probe=ok_probe, store=db(), listening=lambda h, p: False))
    assert checks["toolsets"].status == "fail"


def test_doctor_warns_about_the_cron_wrapper(env, db, monkeypatch):
    monkeypatch.setattr(cli, "cron_wrap", lambda: True)
    assert "cron wrapper" in by_name(cli.run_doctor(probe=ok_probe, store=db(), listening=lambda h, p: False))
    monkeypatch.setattr(cli, "cron_wrap", lambda: False)
    assert "cron wrapper" not in by_name(cli.run_doctor(probe=ok_probe, store=db(), listening=lambda h, p: False))


def test_doctor_empty_allowlist_warns(env, db):
    env.values.pop("AT_ALLOWED_USERS")
    checks = by_name(cli.run_doctor(probe=ok_probe, store=db(), listening=lambda h, p: False))
    assert checks["access"].status == "warn"


def test_doctor_selftest_requires_bare_404_for_wrong_secret(env, db):
    calls = []

    def post(url, form):
        calls.append(url)
        return (200, "END hi") if SECRET in url else (200, "oops")

    checks = by_name(cli.run_doctor(probe=ok_probe, store=db(), post=post, listening=lambda h, p: True))
    assert checks["callback self-test"].status == "fail"
    assert len(calls) == 2


def test_doctor_selftest_passes_when_wired_correctly(env, db):
    def post(url, form):
        return (200, "END hi") if SECRET in url else (404, "")

    checks = by_name(cli.run_doctor(probe=ok_probe, store=db(), post=post, listening=lambda h, p: True))
    assert checks["callback self-test"].status == "ok"


def test_doctor_public_url_must_be_https(env, db):
    checks = by_name(cli.run_doctor("http://x.example.com", probe=ok_probe, store=db(),
                                    listening=lambda h, p: False))
    assert checks["public URL"].status == "fail"


def test_doctor_exit_code_reflects_failures(env, db, monkeypatch):
    monkeypatch.setattr(cli, "_probe", lambda s: Outcome("auth"))
    monkeypatch.setattr(cli, "_listening", lambda h, p: False)
    out = io.StringIO()
    assert cli.cmd_doctor(ns(), out) == 1
    assert "FAIL" in out.getvalue()


def test_doctor_output_never_contains_the_secret_or_api_key(env, db, monkeypatch):
    monkeypatch.setattr(cli, "_probe", ok_probe)
    monkeypatch.setattr(cli, "_listening", lambda h, p: False)
    out = io.StringIO()
    cli.cmd_doctor(ns(), out)
    assert SECRET not in out.getvalue() and "k" * 20 not in out.getvalue()


# -- status / purge ----------------------------------------------------------------------------

def test_status_prints_counts_without_secrets(env, db):
    s = db()
    s.enqueue(A, "r1", ["hello"])
    s.close()
    out = io.StringIO()
    assert cli.cmd_status(ns(), out) == 0
    assert "queued=1" in out.getvalue()
    assert SECRET not in out.getvalue()


def test_status_works_when_unconfigured(env, db):
    env.values.clear()
    out = io.StringIO()
    assert cli.cmd_status(ns(), out) == 0
    assert "outbox" in out.getvalue()


def test_purge_number_erases_and_keeps_optout(env, db):
    s = db()
    s.enqueue(A, "r1", ["hello"])
    s.opt_out(A)
    s.close()
    out = io.StringIO()
    assert cli.cmd_purge(ns(number=A), out) == 0
    s = db()
    assert s.is_opted_out(A)
    assert s.status_counts() == {}
    s.close()
    assert A not in out.getvalue()  # masked
    cli.cmd_purge(ns(number=A, include_optout=True), io.StringIO())
    s = db()
    assert not s.is_opted_out(A)
    s.close()


def test_purge_rejects_bad_number(env, db):
    assert cli.cmd_purge(ns(number="nope"), io.StringIO()) == 2


def test_purge_by_age_runs(env, db):
    out = io.StringIO()
    assert cli.cmd_purge(ns(days=1), out) == 0
    assert "Purged" in out.getvalue()


# -- simulate ----------------------------------------------------------------------------------

def test_simulate_single_hop_and_sms(env):
    seen = []

    def post(url, form):
        seen.append((url, form))
        return 200, "CON Welcome"

    out = io.StringIO()
    assert cli.cmd_simulate(ns(number="0700000001", path=""), out, post=post) == 0
    assert "CON Welcome" in out.getvalue()
    assert seen[0][0].endswith(f"/{SECRET}/ussd") and seen[0][1]["phoneNumber"] == A
    out = io.StringIO()
    assert cli.cmd_simulate(ns(number=A, sms="hello"), out, post=post) == 0
    assert seen[1][0].endswith("/sms") and seen[1][1]["from"] == A


def test_simulate_interactive_walks_the_menu_until_end(env):
    screens = iter(["CON Menu", "CON Type it", "END Sent"])
    paths = []

    def post(url, form):
        paths.append(form["text"])
        return 200, next(screens)

    answers = iter(["1", "hello"])
    out = io.StringIO()
    assert cli.cmd_simulate(ns(number=A), out, post=post, reader=lambda _p: next(answers)) == 0
    assert paths == ["", "1", "1*hello"]
    assert "(session ended)" in out.getvalue()


def test_simulate_reports_http_errors_and_bad_input(env):
    out = io.StringIO()
    cli.cmd_simulate(ns(number=A, path=""), out, post=lambda u, f: (404, ""))
    assert "HTTP 404" in out.getvalue()
    assert cli.cmd_simulate(ns(number="x", path=""), io.StringIO()) == 2
    env.values.clear()
    assert cli.cmd_simulate(ns(number=A, path=""), io.StringIO()) == 2


def test_simulate_without_terminal_requires_path(env, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.cmd_simulate(ns(number=A), io.StringIO(), post=lambda u, f: (200, "CON x")) == 2


# -- wiring ------------------------------------------------------------------------------------

def test_parser_and_dispatch(env, db, capsys):
    p = cli.build_parser()
    assert p.parse_args(["simulate", "--number", A, "--path", "1"]).path == "1"
    assert cli.dispatch(argparse.Namespace(ussd_cmd=None)) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_ussd_slash_command(env, db):
    assert "hermes-ussd status" in cli.ussd_command("")
    assert cli.ussd_command("bogus").startswith("Usage")


# -- sender (target validation is a security control) -------------------------------------------

pytest.importorskip("gateway.platforms._shared")
from hermes_ussd import sender  # noqa: E402


@pytest.fixture
def send_env(monkeypatch):
    values = {"AT_USERNAME": "sandbox", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET,
              "AT_ALLOWED_USERS": A, "AT_HOME_CHANNEL": "+254700000077"}
    monkeypatch.setattr(sender, "_get", values.get)
    return values


def test_validate_target_allows_only_allowlist_and_home(send_env):
    assert sender.validate_target(A) is True
    assert sender.validate_target("0700000001") is True
    assert sender.validate_target("+254700000077") is True
    assert sender.validate_target("+254711111111") is not True
    assert sender.validate_target("garbage") is not True
    assert sender.validate_target("") is not True


def test_validate_target_open_only_when_allow_all(send_env):
    send_env["AT_ALLOW_ALL_USERS"] = "true"
    assert sender.validate_target("+254711111111") is True
    assert sender.validate_target("garbage") is not True


def test_empty_allowlist_refuses_everyone(send_env):
    send_env.pop("AT_ALLOWED_USERS")
    send_env.pop("AT_HOME_CHANNEL")
    assert sender.validate_target(A) is not True


def test_parse_target(send_env):
    assert sender.parse_target("0700000001") == (A, None)
    assert sender.parse_target("nope") is None


@pytest.fixture
async def fake_at(monkeypatch, tmp_path):
    fake = await FakeAT().start()
    path = tmp_path / "sender.db"

    def open_store():
        s = Store(sqlite3.connect(path, check_same_thread=False))
        s.migrate()
        return s

    monkeypatch.setattr(adapter, "open_store", open_store)
    monkeypatch.setattr(adapter, "make_client", lambda s: ATClient(
        s.username, s.api_key, base_url=fake.url, allow_hosts=frozenset({"127.0.0.1"}), http=httpx.AsyncClient()))
    yield fake
    await fake.stop()


async def test_standalone_send_delivers_through_the_outbox(send_env, fake_at):
    res = await sender.standalone_send(None, A, "**Hello** from cron")
    assert res["success"] is True
    assert fake_at.sent == [(A, "Hello from cron")]


async def test_standalone_send_refuses_unlisted_number(send_env, fake_at):
    res = await sender.standalone_send(None, "+254711111111", "spam")
    assert not res.get("success")
    assert fake_at.requests == 0


async def test_standalone_send_honours_opt_out(send_env, fake_at):
    s = adapter.open_store()
    s.opt_out(A)
    s.close()
    res = await sender.standalone_send(None, A, "hi")
    assert not res.get("success")
    assert fake_at.sent == []


async def test_standalone_send_unconfigured_is_a_clean_error(send_env, fake_at):
    send_env.pop("AT_API_KEY")
    res = await sender.standalone_send(None, A, "hi")
    assert not res.get("success") and "not configured" in res["error"]


async def test_standalone_send_reports_partial_failure(send_env, fake_at):
    fake_at.fail_rate, fake_at.fail_mode = 1.0, "reject"
    res = await sender.standalone_send(None, A, "hi")
    assert not res.get("success") and fake_at.sent == []
