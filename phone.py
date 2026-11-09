"""Phone-number helpers: E.164 normalisation and log-safe masking. Pure, no Hermes imports."""

from __future__ import annotations

import re

_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_STRIP = re.compile(r"[\s\-().]")

DEFAULT_COUNTRY_CODE = "254"  # Kenya


def normalize_e164(raw: object, default_cc: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """Return ``raw`` as E.164 (``+254700000001``) or ``None`` when it is not a plausible number.

    Accepts ``+254...``, ``254...``, ``00254...`` and national ``07...``/``7...`` forms (the last two
    are completed with ``default_cc``). Anything else, including text with letters, is rejected.
    """
    if not isinstance(raw, str):
        return None
    s = _STRIP.sub("", raw.strip())
    if not s or len(s) > 20:
        return None
    if s.startswith("+"):
        candidate = s
    elif s.startswith("00"):
        candidate = "+" + s[2:]
    elif s.startswith("0"):
        candidate = "+" + default_cc + s[1:]
    elif s.isdigit() and s.startswith(default_cc):
        candidate = "+" + s
    elif s.isdigit() and len(s) == 9:
        candidate = "+" + default_cc + s
    else:
        candidate = "+" + s if s.isdigit() else ""
    return candidate if _E164.match(candidate) else None


def mask(number: object) -> str:
    """``+254700000001`` -> ``+2547••••0001``. Never raises; unknown input becomes ``***``."""
    if not isinstance(number, str) or len(number) < 8:
        return "***"
    return f"{number[:5]}••••{number[-4:]}"


def parse_allowlist(raw: object, default_cc: str = DEFAULT_COUNTRY_CODE) -> frozenset[str]:
    """Comma/space separated numbers -> normalised set; entries that do not parse are dropped."""
    if not isinstance(raw, str):
        return frozenset()
    out = set()
    for part in re.split(r"[,\s;]+", raw):
        if not part:
            continue
        n = normalize_e164(part, default_cc)
        if n:
            out.add(n)
    return frozenset(out)
