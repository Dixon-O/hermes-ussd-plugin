from hypothesis import given, settings as hset, strategies as hs

from hermes_ussd.sms import encoding as enc


def test_plain_ascii_untouched():
    assert enc.normalize("Hello, world 123?") == "Hello, world 123?"


def test_smart_punctuation_folded():
    assert enc.normalize("It’s “fine” — really…") == "It's \"fine\" - really..."


def test_emoji_dropped_and_stays_gsm7():
    out = enc.normalize("great \U0001F600 job \U0001F44D")
    assert out == "great job"
    assert enc.is_gsm7(out)


def test_accents_fold_when_not_in_gsm():
    assert enc.normalize("crème brûlée") == "crème brulée"  # è kept, û -> u, é kept


def test_unknown_symbol_becomes_question_mark():
    assert enc.normalize("x 中 y") == "x ? y"


def test_extension_chars_cost_two():
    assert enc.septets("[a]") == 5
    assert enc.septets("€10") == 4


def test_single_segment_boundary():
    assert enc.segment_count("a" * 160) == 1
    assert enc.segment_count("a" * 161) == 2
    assert enc.segment_count("a" * 306) == 2
    assert enc.segment_count("a" * 307) == 3
    assert enc.segment_count("") == 0


def test_split_short_is_one_segment():
    s = enc.split_segments("hello there")
    assert s.segments == ("hello there",) and not s.truncated


def test_split_empty_is_no_segments():
    assert enc.split_segments("   \U0001F600  ").segments == ()


def test_split_respects_words_and_limits():
    text = " ".join(["word%03d" % i for i in range(60)])  # ~480 chars
    s = enc.split_segments(text, max_segments=4)
    assert 1 < len(s.segments) <= 4 and not s.truncated
    assert all(enc.septets(seg) <= enc.MULTI_LIMIT for seg in s.segments)
    assert " ".join(s.segments).split() == text.split()  # no word cut in half


def test_truncation_adds_note_and_caps_segments():
    s = enc.split_segments("x" * 2000, max_segments=3, truncation_note=" ...[more: USSD 2]")
    assert s.truncated and len(s.segments) <= 3
    assert s.segments[-1].endswith("[more: USSD 2]")
    assert s.total_septets == 2000


def test_extension_char_pair_never_split():
    text = "{" * 400  # every char costs two septets
    s = enc.split_segments(text, max_segments=6)
    for seg in s.segments:
        assert enc.septets(seg) <= enc.MULTI_LIMIT
        assert set(seg) <= {"{"}


@given(hs.text(max_size=3000), hs.integers(min_value=1, max_value=6))
@hset(max_examples=300, deadline=None)
def test_split_invariants_on_arbitrary_text(text, max_segments):
    s = enc.split_segments(text, max_segments=max_segments, truncation_note="...")
    assert len(s.segments) <= max_segments
    for seg in s.segments:
        assert seg and enc.is_gsm7(seg)
        limit = enc.SINGLE_LIMIT if len(s.segments) == 1 else enc.MULTI_LIMIT
        assert enc.septets(seg) <= limit


@given(hs.text(max_size=500))
def test_normalize_is_idempotent_and_gsm(text):
    once = enc.normalize(text)
    assert enc.is_gsm7(once)
    assert enc.normalize(once) == once
