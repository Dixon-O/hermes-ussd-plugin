import pytest
from hypothesis import given, strategies as hs

from hermes_ussd import phone
from hermes_ussd.limits import Limiter, per_hour, per_minute
from hermes_ussd.settings import ConfigError, load_settings

from conftest import SECRET


@pytest.mark.parametrize("raw,expected", [
    ("+254700000001", "+254700000001"),
    ("254700000001", "+254700000001"),
    ("0700000001", "+254700000001"),
    ("0700 000 001", "+254700000001"),
    ("700000001", "+254700000001"),
    ("00254700000001", "+254700000001"),
    ("+1 (415) 555-0123", "+14155550123"),
])
def test_normalize_ok(raw, expected):
    assert phone.normalize_e164(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "+", "+0123456789", "12", "+254" + "9" * 20, None, 123, "07x0000001"])
def test_normalize_rejects(raw):
    assert phone.normalize_e164(raw) is None


@given(hs.text(max_size=40))
def test_normalize_never_raises_and_is_e164_or_none(raw):
    out = phone.normalize_e164(raw)
    assert out is None or (out.startswith("+") and out[1:].isdigit() and 8 <= len(out) <= 16)


def test_mask_hides_middle():
    assert phone.mask("+254700000001") == "+2547••••0001"
    assert phone.mask("") == "***" and phone.mask(None) == "***"


def test_allowlist_parsing_drops_garbage():
    got = phone.parse_allowlist("+254700000001, 0711111111;junk  254722222222")
    assert got == {"+254700000001", "+254711111111", "+254722222222"}


def test_limiter_burst_then_refill():
    t = [0.0]
    lim = Limiter(burst=3, per_second=1.0, clock=lambda: t[0])
    assert [lim.allow("a") for _ in range(4)] == [True, True, True, False]
    t[0] += 2
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a")
    assert lim.allow("b")  # independent key


def test_limiter_bounds_memory():
    t = [0.0]
    lim = Limiter(burst=1, per_second=1.0, max_keys=100, clock=lambda: t[0])
    for i in range(1000):
        lim.allow(f"k{i}")
    assert len(lim) <= 100


def test_limiter_helpers():
    assert per_minute(2).allow("x") and per_hour(1).allow("x")
    with pytest.raises(ValueError):
        Limiter(0, 1)


def _env(**over):
    base = {"AT_USERNAME": "sandbox", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET}
    base.update(over)
    return base.get


def test_settings_defaults_are_safe():
    s = load_settings(_env())
    assert s.sandbox and s.host == "127.0.0.1" and s.port == 8790
    assert not s.allow_all and not s.allow_commands and s.toolsets == ("safe",)
    assert s.api_host == "api.sandbox.africastalking.com"
    assert "kkkk" not in repr(s) and SECRET not in repr(s)  # secrets never reach logs via repr


@pytest.mark.parametrize("missing", ["AT_USERNAME", "AT_API_KEY", "AT_WEBHOOK_SECRET"])
def test_settings_require_credentials(missing):
    env = {"AT_USERNAME": "u", "AT_API_KEY": "k" * 20, "AT_WEBHOOK_SECRET": SECRET}
    env.pop(missing)
    with pytest.raises(ConfigError):
        load_settings(env.get)


@pytest.mark.parametrize("weak", ["short", "has space " + "x" * 30, "slash/" + "x" * 30, "x" * 200, "é" * 30])
def test_settings_reject_weak_secret(weak):
    with pytest.raises(ConfigError):
        load_settings(_env(AT_WEBHOOK_SECRET=weak))


def test_settings_toolsets_and_bounds():
    assert load_settings(_env(AT_TOOLSETS="default")).toolsets is None
    assert load_settings(_env(AT_TOOLSETS="web, memory")).toolsets == ("web", "memory")
    s = load_settings(_env(AT_SMS_MAX_SEGMENTS="99", AT_WEBHOOK_PORT="abc", AT_ENV="production"))
    assert s.sms_max_segments == 6 and s.port == 8790 and not s.sandbox
    with pytest.raises(ConfigError):
        load_settings(_env(AT_ENV="live"))


def test_settings_falls_back_to_yaml_extra():
    s = load_settings({"AT_USERNAME": "u", "AT_API_KEY": "k" * 20}.get,
                      {"webhook_secret": SECRET, "daily_sms_cap": 5})
    assert s.daily_sms_cap == 5
