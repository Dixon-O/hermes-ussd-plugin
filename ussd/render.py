"""USSD screen rendering: CON/END framing, hard length cap, pagination. Pure, no Hermes imports.

Africa's Talking (like most gateways) ends the session if a reply is longer than 182 characters or does
not start with CON/END. Every screen is therefore built here and clamped, so no code path can emit a
malformed response.
"""

from __future__ import annotations

import re
import unicodedata

MAX_SCREEN = 182  # hard limit for the whole reply, "CON "/"END " included
PAGE_BODY = 120  # characters of stored answer per read page (leaves room for footer + counter)

_CTRL = re.compile(r"[^\S\n ]+")  # whitespace other than space/newline (tabs, CR, NBSP, ...)


def clean(text: object) -> str:
    """Printable text only: control/format characters removed, runs of blanks collapsed."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    kept = []
    for ch in text:
        if ch == "\n" or ch == " ":
            kept.append(ch)
        elif unicodedata.category(ch)[0] == "C":
            continue
        elif ch.isspace():
            kept.append(" ")
        else:
            kept.append(ch)
    out = "".join(kept)
    out = re.sub(r" {2,}", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()


def _frame(verb: str, body: object) -> str:
    cleaned = clean(body)
    room = MAX_SCREEN - len(verb) - 1
    if len(cleaned) > room:
        cleaned = cleaned[:room].rstrip()
    return f"{verb} {cleaned}" if cleaned else verb


def con(body: object) -> str:
    """A screen that keeps the session open and waits for input."""
    return _frame("CON", body)


def end(body: object) -> str:
    """A final screen: the session closes after it is shown."""
    return _frame("END", body)


def paginate(text: str, body_limit: int = PAGE_BODY) -> list[str]:
    """Split ``text`` into pages of at most ``body_limit`` characters, breaking at spaces."""
    text = clean(text).replace("\n", " ")
    text = re.sub(r" {2,}", " ", text).strip()
    if not text:
        return []
    pages: list[str] = []
    while text:
        if len(text) <= body_limit:
            pages.append(text)
            break
        cut = text.rfind(" ", 0, body_limit + 1)
        if cut < body_limit * 0.5:
            cut = body_limit
        pages.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return pages


def is_valid_screen(screen: str) -> bool:
    """True when ``screen`` is something a USSD gateway will accept."""
    if len(screen) > MAX_SCREEN or not screen:
        return False
    head = screen.split(" ", 1)[0].split("\n", 1)[0]
    return head in ("CON", "END") and all(ch == "\n" or unicodedata.category(ch)[0] != "C" for ch in screen)
