"""The USSD menu as a pure state machine. No I/O, no Hermes imports.

Africa's Talking sends the *whole* input path on every hop (``""``, ``"1"``, ``"1*what is a loan"``,
``"2*98"``...), so the screen is a pure function of that path plus a small read-only snapshot of the
caller's state. Anything with a side effect (queueing a question, saving a language) comes back as a
field on :class:`Step` for the service layer to apply, which keeps this module trivially testable.

Navigation keys: ``0`` back, ``00`` main menu, ``98`` next page. Once the user picks "Ask", the rest
of the path is the question verbatim, ``*`` characters included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from . import render
from .strings import normalize_lang, t

MAX_PATH = 400  # the service caps hop text well below this; a belt-and-braces bound here
MAX_QUESTION = 200


class MenuContext(Protocol):
    lang: str
    last_answer: Optional[str]
    pending: int
    sent_today: int
    daily_cap: int


@dataclass(frozen=True)
class Step:
    """What the service should do for one USSD hop."""

    screen: str  # complete CON/END reply; empty when ``ask`` is set (service renders the receipt)
    final: bool = False
    ask: Optional[str] = None  # a question to queue; service allocates the ref and renders the receipt
    set_lang: Optional[str] = None  # a language to persist


def ask_receipt(lang: object, ref: str) -> str:
    """The END screen shown once a question is queued."""
    return render.end(t(lang, "ask_done", ref=ref))


def _main(ctx: MenuContext, note: Optional[str] = None) -> Step:
    body = t(ctx.lang, "main")
    if note:
        body = f"{note}\n{body}"
    return Step(render.con(body))


def _read(ctx: MenuContext, page: int, note: Optional[str]) -> Step:
    pages = render.paginate(ctx.last_answer or "")
    if not pages:
        return Step(render.con(t(ctx.lang, "read_none")))
    page = max(0, min(page, len(pages) - 1))
    last = page == len(pages) - 1
    footer = t(ctx.lang, "read_nav_last" if last else "read_nav_more")
    head = f"{page + 1}/{len(pages)} {pages[page]}"
    if note:
        head = f"{note} {head}"
    return Step(render.con(f"{head}\n{footer}"))


def _render(state: tuple, ctx: MenuContext, note: Optional[str]) -> Step:
    kind = state[0]
    if kind == "main":
        return _main(ctx, note)
    if kind == "ask":
        return Step(render.con(t(ctx.lang, "ask_prompt")))
    if kind == "read":
        return _read(ctx, state[1], note)
    if kind == "status":
        body = t(ctx.lang, "status", pending=ctx.pending, sent=ctx.sent_today, cap=ctx.daily_cap)
        return Step(render.con(f"{note}\n{body}" if note else body))
    if kind == "lang":
        body = t(ctx.lang, "lang_menu")
        return Step(render.con(f"{note}\n{body}" if note else body))
    if kind == "help":
        body = t(ctx.lang, "help")
        return Step(render.con(f"{note}\n{body}" if note else body))
    return Step(render.end(t(ctx.lang, "error")), final=True)


def _transition(state: tuple, tok: str, ctx: MenuContext) -> tuple[tuple, Optional[str], Optional[Step]]:
    """One input applied to one state: ``(next_state, note, terminal_step)``."""
    kind = state[0]
    invalid = t(ctx.lang, "invalid")
    if kind == "main":
        nxt = {"1": ("ask",), "2": ("read", 0), "3": ("status",), "4": ("lang",), "5": ("help",)}.get(tok)
        return (nxt, None, None) if nxt else (state, invalid, None)
    if kind == "read":
        if tok == "98":
            return (("read", state[1] + 1), None, None)
        if tok == "0":
            return (("read", state[1] - 1), None, None) if state[1] > 0 else (("main",), None, None)
        if tok == "00":
            return (("main",), None, None)
        return (state, invalid, None)
    if kind in ("status", "help"):
        return (("main",), None, None) if tok in ("0", "00") else (state, invalid, None)
    if kind == "lang":
        if tok in ("1", "2"):
            lang = "en" if tok == "1" else "sw"
            return (("done",), None, Step(render.end(t(lang, "lang_set")), final=True, set_lang=lang))
        if tok in ("0", "00"):
            return (("main",), None, None)
        return (state, invalid, None)
    return (state, invalid, None)


def step(path: object, ctx: MenuContext) -> Step:
    """The reply for one USSD hop. ``path`` is the gateway's ``text`` field (``""`` on the first hop)."""
    if not isinstance(path, str):
        path = ""
    path = path.strip()
    if len(path) > MAX_PATH:
        return Step(render.end(t(ctx.lang, "error")), final=True)
    ctx_lang = normalize_lang(getattr(ctx, "lang", None))
    if ctx_lang != ctx.lang:  # tolerate a bad stored value without trusting it
        ctx = _LangOverride(ctx, ctx_lang)

    tokens = path.split("*") if path else []
    state: tuple = ("main",)
    note: Optional[str] = None
    for i, raw in enumerate(tokens):
        if state[0] == "ask":
            question = "*".join(tokens[i:]).strip()
            if question == "0":
                state, note = ("main",), None
                break
            if not question:
                return Step(render.con(t(ctx.lang, "ask_empty")))
            return Step("", final=True, ask=question[:MAX_QUESTION])
        state, note, terminal = _transition(state, raw.strip(), ctx)
        if terminal is not None:
            if i < len(tokens) - 1:  # input after a final screen: that session is over
                return Step(render.end(t(ctx.lang, "session_over")), final=True)
            return terminal
        if state[0] == "done":
            return Step(render.end(t(ctx.lang, "session_over")), final=True)
    return _render(state, ctx, note)


class _LangOverride:
    """Read-through wrapper that forces a valid language onto a context."""

    def __init__(self, inner: MenuContext, lang: str) -> None:
        self._inner, self.lang = inner, lang

    def __getattr__(self, name: str):
        return getattr(self._inner, name)
