"""GSM 03.38 (GSM-7) normalisation and SMS segment maths. Pure, no Hermes imports.

Why this exists: a single emoji or smart quote flips an SMS to UCS-2, cutting a segment from 160 to
70 characters and tripling the price of an answer. Replies are normalised to GSM-7 *before* they are
counted, so the cost of an answer is predictable and bounded.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SINGLE_LIMIT = 160  # septets in one unconcatenated SMS
MULTI_LIMIT = 153  # septets per segment when concatenated (UDH takes 7)

# GSM 03.38 default alphabet, without the escape slot (0x1B).
_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x00ÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)
GSM_BASIC = frozenset(_BASIC) - {"\x00"}
# Extension table: each of these costs two septets (ESC + code).
GSM_EXT = frozenset("\f^{}\\[~]|€")
GSM_ALL = GSM_BASIC | GSM_EXT

# Typographic characters that have a clean GSM-7 equivalent.
_MAP = {
    "‘": "'", "’": "'", "‚": ",", "‛": "'",
    "“": '"', "”": '"', "„": '"', "′": "'", "″": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    "…": "...", " ": " ", " ": " ", " ": " ", " ": " ",
    "•": "-", "·": "-", "→": "->", "←": "<-", "×": "x", "÷": "/",
    "✓": "ok", "✔": "ok", "✗": "x", "✘": "x",
    "\t": " ", "«": '"', "»": '"', "™": "(tm)", "©": "(c)", "®": "(r)",
    "°": " deg", "½": "1/2", "¼": "1/4", "¾": "3/4",
    "≤": "<=", "≥": ">=", "≠": "!=", "±": "+/-",
    "Œ": "OE", "œ": "oe", "Ł": "L", "ł": "l", "Đ": "D", "đ": "d",
}
_DROP_CATEGORIES = {"So", "Sk", "Cs", "Cf", "Mn", "Me", "Co", "Cn", "Cc"}
_WS_RUN = re.compile(r"[ ]{2,}")
_NL_RUN = re.compile(r"\n{3,}")


def _fold(ch: str) -> str:
    """Best-effort GSM-7 form of one character; ``""`` drops it, ``"?"`` marks the unknown."""
    if ch in _MAP:
        return _MAP[ch]
    if ch in GSM_ALL:
        return ch
    if ch in ("\r\n",):
        return "\n"
    decomposed = unicodedata.normalize("NFKD", ch)
    if decomposed != ch:
        folded = "".join(c for c in decomposed if not unicodedata.combining(c))
        if folded and all(c in GSM_ALL for c in folded):
            return folded
    if unicodedata.category(ch) in _DROP_CATEGORIES:
        return ""
    return "?"


def normalize(text: object) -> str:
    """Return ``text`` restricted to GSM-7, with whitespace tidied. Never raises on odd input."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = "".join(_fold(ch) for ch in text)
    out = out.replace("\x00", "")
    out = _WS_RUN.sub(" ", out)
    out = _NL_RUN.sub("\n\n", out)
    return out.strip()


def septets(text: str) -> int:
    """Septets ``text`` occupies (extension characters cost two). Assumes GSM-7 input."""
    return sum(2 if ch in GSM_EXT else 1 for ch in text)


def is_gsm7(text: str) -> bool:
    return all(ch in GSM_ALL for ch in text)


def segment_count(text: str) -> int:
    """Number of SMS segments ``text`` costs once normalised (0 for empty)."""
    n = septets(text)
    if n == 0:
        return 0
    return 1 if n <= SINGLE_LIMIT else -(-n // MULTI_LIMIT)


@dataclass(frozen=True)
class Split:
    segments: tuple[str, ...]
    truncated: bool
    total_septets: int  # of the *original* normalised text


def _cut(text: str, budget: int) -> str:
    """Longest prefix of ``text`` within ``budget`` septets (never splits an extension pair)."""
    used = 0
    for i, ch in enumerate(text):
        cost = 2 if ch in GSM_EXT else 1
        if used + cost > budget:
            return text[:i]
        used += cost
    return text


def _chunk(text: str, limit: int, prefer_breaks: bool) -> list[str]:
    chunks: list[str] = []
    rest = text
    while rest:
        if septets(rest) <= limit:
            chunks.append(rest)
            break
        head = _cut(rest, limit)
        if prefer_breaks:
            # Break at the last newline/space in the back 40% of the chunk, so words stay whole.
            floor = int(len(head) * 0.6)
            idx = max(head.rfind("\n"), head.rfind(" "))
            if idx > floor:
                head = head[:idx]
        chunks.append(head.rstrip())
        rest = rest[len(head):].lstrip(" \n")
    return [c for c in chunks if c]


def split_segments(text: str, max_segments: int = 3, truncation_note: str = "...") -> Split:
    """Normalise ``text`` and split it into at most ``max_segments`` GSM-7 SMS bodies.

    A reply that does not fit is cut and ends with ``truncation_note`` (the caller points the reader
    at the USSD "read last answer" screen, which always holds the full text). Guarantees:
    every segment is pure GSM-7; a single segment is at most 160 septets and each concatenated
    segment at most 153; ``len(segments) <= max_segments``.
    """
    max_segments = max(1, int(max_segments))
    clean = normalize(text)
    total = septets(clean)
    if total == 0:
        return Split((), False, 0)
    if total <= SINGLE_LIMIT:
        return Split((clean,), False, total)

    # One septet of slack per segment keeps hard cuts from overrunning when a 2-septet char
    # lands on a boundary.
    budget = max_segments * (MULTI_LIMIT - 1)
    truncated = total > budget
    body = clean
    if truncated:
        note = normalize(truncation_note)
        body = _cut(clean, budget - septets(note)).rstrip() + note
    chunks = _chunk(body, MULTI_LIMIT, prefer_breaks=True)
    if len(chunks) > max_segments:
        chunks = _chunk(body, MULTI_LIMIT, prefer_breaks=False)
    return Split(tuple(chunks), truncated, total)
