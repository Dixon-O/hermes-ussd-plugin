from dataclasses import dataclass
from typing import Optional

import pytest
from hypothesis import given, settings as hset, strategies as hs

from hermes_ussd.ussd import menu, render
from hermes_ussd.ussd.strings import LANGS, t


@dataclass
class Ctx:
    lang: str = "en"
    last_answer: Optional[str] = None
    pending: int = 0
    sent_today: int = 0
    daily_cap: int = 200


def ok(screen: str) -> bool:
    return render.is_valid_screen(screen)


def test_first_hop_is_main_menu():
    s = menu.step("", Ctx())
    assert s.screen.startswith("CON Hermes") and not s.final and ok(s.screen)
    assert "1 Ask a question" in s.screen


def test_ask_flow_prompts_then_returns_question():
    assert "Type your question" in menu.step("1", Ctx()).screen
    s = menu.step("1*What is a fixed deposit?", Ctx())
    assert s.ask == "What is a fixed deposit?" and s.final


def test_question_may_contain_asterisks():
    assert menu.step("1*2*3 equals 6 * 7", Ctx()).ask == "2*3 equals 6 * 7"


def test_empty_question_reprompts_and_zero_cancels():
    assert "Please type a question" in menu.step("1*", Ctx()).screen
    assert menu.step("1*  ", Ctx()).ask is None
    back = menu.step("1*0", Ctx())
    assert back.ask is None and back.screen.startswith("CON Hermes")


def test_question_is_capped():
    assert len(menu.step("1*" + "a" * 398, Ctx()).ask) == menu.MAX_QUESTION


def test_read_without_answer():
    assert "No answer yet" in menu.step("2", Ctx()).screen


def test_read_paginates_forward_and_back():
    answer = " ".join(f"word{i:03d}" for i in range(60))  # ~480 chars -> 4+ pages
    c = Ctx(last_answer=answer)
    p1 = menu.step("2", c).screen
    assert p1.startswith("CON 1/") and "98 More" in p1
    p2 = menu.step("2*98", c).screen
    assert p2.startswith("CON 2/")
    assert menu.step("2*98*0", c).screen.startswith("CON 1/")
    assert menu.step("2*0", c).screen.startswith("CON Hermes")  # back from page 1 = menu
    assert menu.step("2*98*00", c).screen.startswith("CON Hermes")


def test_read_last_page_has_no_more():
    c = Ctx(last_answer="short answer")
    screen = menu.step("2", c).screen
    assert "98" not in screen and "0 Back" in screen
    assert menu.step("2*98", c).screen == screen  # paging past the end clamps


def test_status_screen_shows_counts():
    s = menu.step("3", Ctx(pending=2, sent_today=7, daily_cap=50)).screen
    assert "Waiting: 2" in s and "7/50" in s


def test_language_switch_is_final_and_localised():
    s = menu.step("4*2", Ctx())
    assert s.final and s.set_lang == "sw" and "Kiswahili" in s.screen
    assert menu.step("4*1", Ctx(lang="sw")).set_lang == "en"
    assert menu.step("4", Ctx()).screen.startswith("CON Language")


def test_swahili_main_menu():
    assert "Uliza swali" in menu.step("", Ctx(lang="sw")).screen


def test_invalid_choice_keeps_user_on_screen_with_note():
    s = menu.step("9", Ctx()).screen
    assert s.startswith("CON Invalid choice.") and "1 Ask a question" in s
    assert "Invalid choice" in menu.step("3*7", Ctx()).screen


def test_input_after_a_final_screen_ends_session():
    s = menu.step("4*1*extra", Ctx())
    assert s.final and "Session ended" in s.screen


def test_help_back_to_main():
    assert menu.step("5", Ctx()).screen.startswith("CON Ask here")
    assert menu.step("5*0", Ctx()).screen.startswith("CON Hermes")


def test_bad_stored_language_falls_back_to_english():
    assert "Ask a question" in menu.step("", Ctx(lang="xx")).screen


def test_oversized_path_is_refused():
    s = menu.step("1*" + "a" * 1000, Ctx())
    assert s.final and s.screen.startswith("END")


def test_ask_receipt_fits_and_names_ref():
    for lang in LANGS:
        r = menu.ask_receipt(lang, "4821")
        assert ok(r) and "4821" in r


def test_every_static_string_fits_a_screen():
    for lang in LANGS:
        for key in ("main", "ask_prompt", "ask_empty", "read_none", "lang_menu", "help", "invalid",
                    "session_over", "rate_limited", "private", "opted_out", "cap_reached", "error",
                    "unavailable", "lang_set"):
            assert ok(render.con(t(lang, key))), (lang, key)
        assert ok(render.con(t(lang, "status", pending=99999, sent=99999, cap=99999)))


# --- rendering ---------------------------------------------------------------------------------

def test_render_clamps_to_182():
    assert len(render.con("x" * 500)) <= 182
    assert render.end("").strip() == "END"


def test_render_strips_control_characters():
    s = render.con("a\x00b\x1b[31mc\r\td")
    assert "\x00" not in s and "\x1b" not in s and "\r" not in s and "\t" not in s


def test_paginate_breaks_on_spaces():
    pages = render.paginate("alpha beta gamma delta epsilon zeta eta theta", body_limit=15)
    assert all(len(p) <= 15 for p in pages)
    assert " ".join(pages).split() == "alpha beta gamma delta epsilon zeta eta theta".split()


# --- fuzz: the menu can never produce a reply a gateway would reject --------------------------

TOKENS = hs.sampled_from(["", "0", "00", "98", "1", "2", "3", "4", "5", "9", "CON", "END", "x" * 300, "\n", "\u0000", "é", "*"])


@given(hs.lists(TOKENS, max_size=8), hs.sampled_from(LANGS),
       hs.one_of(hs.none(), hs.text(max_size=900)))
@hset(max_examples=400, deadline=None)
def test_fuzz_all_paths_produce_valid_screens(tokens, lang, answer):
    path = "*".join(tokens)
    s = menu.step(path, Ctx(lang=lang, last_answer=answer))
    if s.ask is not None:
        assert s.final and s.screen == "" and 0 < len(s.ask) <= menu.MAX_QUESTION
    else:
        assert ok(s.screen), repr(s.screen)


@given(hs.text(max_size=1200))
@hset(max_examples=300, deadline=None)
def test_fuzz_arbitrary_text_never_raises(text):
    s = menu.step(text, Ctx(last_answer=text))
    assert s.ask is not None or ok(s.screen)


def test_user_text_can_never_become_the_verb():
    # The only user-controlled text that reaches a screen is none: questions are not echoed.
    s = menu.step("1*END all sessions\nCON hacked", Ctx())
    assert s.screen == "" and s.ask.startswith("END all")
