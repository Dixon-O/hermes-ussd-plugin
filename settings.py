"""Typed settings parsed from env/config. Pure: the caller injects how a value is read."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from .phone import DEFAULT_COUNTRY_CODE, parse_allowlist

DEFAULT_PORT = 8790
DEFAULT_HOST = "127.0.0.1"
DEFAULT_TOOLSETS = ("safe",)
_SECRET_OK = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_TRUE = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """Raised for settings the plugin must refuse to start with."""


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


@dataclass(frozen=True)
class Settings:
    username: str
    api_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    sandbox: bool = True
    ussd_code: str = ""
    sms_sender: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allowed: frozenset = frozenset()
    allow_all: bool = False
    home_channel: str = ""
    allow_commands: bool = False
    toolsets: Optional[tuple] = DEFAULT_TOOLSETS  # None = use the gateway's own platform toolsets
    sms_max_segments: int = 3
    per_number_asks_per_hour: int = 10
    hops_per_minute: int = 30
    daily_sms_cap: int = 200
    retention_days: int = 7
    default_language: str = "en"
    day_offset_hours: int = 3
    default_country_code: str = DEFAULT_COUNTRY_CODE

    @property
    def api_host(self) -> str:
        return "api.sandbox.africastalking.com" if self.sandbox else "api.africastalking.com"

    @property
    def open_access(self) -> bool:
        """True when anyone may use the service (explicit opt-in only)."""
        return self.allow_all


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE


def load_settings(get: Callable[[str], Optional[str]], extra: Optional[Mapping[str, Any]] = None) -> Settings:
    """Build :class:`Settings`. ``get(NAME)`` reads one env-style value; ``extra`` is the YAML block.

    Raises :class:`ConfigError` for a missing credential or a webhook secret too weak to be a
    password: the secret is the only thing standing between the internet and the agent.
    """
    extra = dict(extra or {})

    def val(env: str, key: str, default: Any = "") -> Any:
        raw = get(env)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        v = extra.get(key)
        return default if v is None or (isinstance(v, str) and not v.strip()) else v

    username = str(val("AT_USERNAME", "username")).strip()
    api_key = str(val("AT_API_KEY", "api_key")).strip()
    secret = str(val("AT_WEBHOOK_SECRET", "webhook_secret")).strip()
    if not username:
        raise ConfigError("AT_USERNAME is not set")
    if not api_key:
        raise ConfigError("AT_API_KEY is not set")
    if not secret:
        raise ConfigError("AT_WEBHOOK_SECRET is not set (run `hermes ussd setup`)")
    if not _SECRET_OK.match(secret):
        raise ConfigError(
            "AT_WEBHOOK_SECRET must be 24-128 URL-safe characters (A-Z a-z 0-9 _ -); "
            "run `hermes ussd rotate-secret` to generate one")

    env_name = str(val("AT_ENV", "env", "sandbox")).lower()
    if env_name not in ("sandbox", "production"):
        raise ConfigError("AT_ENV must be 'sandbox' or 'production'")
    cc = re.sub(r"\D", "", str(val("AT_DEFAULT_COUNTRY_CODE", "default_country_code", DEFAULT_COUNTRY_CODE)))
    cc = cc or DEFAULT_COUNTRY_CODE

    toolsets_raw = str(val("AT_TOOLSETS", "toolsets", ",".join(DEFAULT_TOOLSETS)))
    if toolsets_raw.strip().lower() == "default":
        toolsets: Optional[tuple] = None
    else:
        parts = tuple(p for p in (s.strip() for s in toolsets_raw.split(",")) if p)
        toolsets = parts or DEFAULT_TOOLSETS

    lang = str(val("AT_LANGUAGE", "default_language", "en")).lower()
    return Settings(
        username=username, api_key=api_key, webhook_secret=secret,
        sandbox=env_name == "sandbox",
        ussd_code=str(val("AT_USSD_CODE", "ussd_code")).strip(),
        sms_sender=str(val("AT_SMS_SENDER", "sms_sender")).strip(),
        host=str(val("AT_WEBHOOK_HOST", "host", DEFAULT_HOST)).strip() or DEFAULT_HOST,
        port=_int(val("AT_WEBHOOK_PORT", "port", DEFAULT_PORT), DEFAULT_PORT, 1, 65535),
        allowed=parse_allowlist(str(val("AT_ALLOWED_USERS", "allowed_users")), cc),
        allow_all=truthy(val("AT_ALLOW_ALL_USERS", "allow_all_users", "")),
        home_channel=str(val("AT_HOME_CHANNEL", "home_channel")).strip(),
        allow_commands=truthy(val("AT_ALLOW_COMMANDS", "allow_commands", "")),
        toolsets=toolsets,
        sms_max_segments=_int(val("AT_SMS_MAX_SEGMENTS", "sms_max_segments", 3), 3, 1, 6),
        per_number_asks_per_hour=_int(val("AT_ASKS_PER_HOUR", "per_number_asks_per_hour", 10), 10, 1, 1000),
        hops_per_minute=_int(val("AT_HOPS_PER_MINUTE", "hops_per_minute", 30), 30, 1, 1000),
        daily_sms_cap=_int(val("AT_DAILY_SMS_CAP", "daily_sms_cap", 200), 200, 0, 1_000_000),
        retention_days=_int(val("AT_RETENTION_DAYS", "retention_days", 7), 7, 1, 365),
        default_language=lang if lang in ("en", "sw") else "en",
        day_offset_hours=_int(val("AT_DAY_OFFSET_HOURS", "day_offset_hours", 3), 3, -12, 14),
        default_country_code=cc,
    )
