"""User-facing USSD strings. Screens are written to fit well inside 160 characters.

Only ``en`` and ``sw`` exist today; an unknown language falls back to English. Swahili text is a
first pass and should be reviewed by a native speaker before a production launch.
"""

from __future__ import annotations

LANGS = ("en", "sw")
DEFAULT_LANG = "en"

_S: dict[str, dict[str, str]] = {
    "en": {
        "main": "Hermes\n1 Ask a question\n2 Read last answer\n3 Status\n4 Language\n5 Help",
        "ask_prompt": "Type your question (0 to cancel):",
        "ask_empty": "Please type a question.\nType your question (0 to cancel):",
        "ask_done": "Got it. Your answer will come by SMS. Ref {ref}. You can also dial back and choose 2.",
        "read_none": "No answer yet.\n0 Back",
        "read_nav_more": "98 More\n0 Back",
        "read_nav_last": "0 Back",
        "status": "Waiting: {pending}\nSMS today: {sent}/{cap}\n0 Back",
        "lang_menu": "Language\n1 English\n2 Kiswahili\n0 Back",
        "lang_set": "Language set to English.",
        "help": "Ask here, get the answer by SMS. SMS STOP to opt out, START to rejoin.\n0 Back",
        "invalid": "Invalid choice.",
        "session_over": "Session ended. Dial again to continue.",
        "rate_limited": "Too many requests. Please try again later.",
        "private": "This service is private.",
        "opted_out": "You opted out of SMS. SMS START to rejoin.",
        "cap_reached": "Daily SMS limit reached. Read your answers by dialing and choosing 2.",
        "error": "Sorry, try again.",
        "unavailable": "Service busy. Try again shortly.",
    },
    "sw": {
        "main": "Hermes\n1 Uliza swali\n2 Soma jibu la mwisho\n3 Hali\n4 Lugha\n5 Msaada",
        "ask_prompt": "Andika swali lako (0 kughairi):",
        "ask_empty": "Tafadhali andika swali.\nAndika swali lako (0 kughairi):",
        "ask_done": "Sawa. Jibu litatumwa kwa SMS. Kumb {ref}. Unaweza pia kupiga tena na kuchagua 2.",
        "read_none": "Hakuna jibu bado.\n0 Rudi",
        "read_nav_more": "98 Zaidi\n0 Rudi",
        "read_nav_last": "0 Rudi",
        "status": "Zinasubiri: {pending}\nSMS leo: {sent}/{cap}\n0 Rudi",
        "lang_menu": "Lugha\n1 English\n2 Kiswahili\n0 Rudi",
        "lang_set": "Lugha: Kiswahili.",
        "help": "Uliza hapa, pata jibu kwa SMS. Tuma STOP kujiondoa, START kujiunga tena.\n0 Rudi",
        "invalid": "Chaguo si sahihi.",
        "session_over": "Kipindi kimeisha. Piga tena kuendelea.",
        "rate_limited": "Maombi mengi. Jaribu tena baadaye.",
        "private": "Huduma hii ni ya faragha.",
        "opted_out": "Umejiondoa kwenye SMS. Tuma START kujiunga tena.",
        "cap_reached": "Kikomo cha SMS cha leo kimefika. Soma majibu kwa kupiga na kuchagua 2.",
        "error": "Samahani, jaribu tena.",
        "unavailable": "Huduma ina shughuli. Jaribu tena baadaye.",
    },
}


def normalize_lang(lang: object) -> str:
    return lang if isinstance(lang, str) and lang in _S else DEFAULT_LANG


def t(lang: object, key: str, **fmt: object) -> str:
    """Look up ``key`` in ``lang`` (English fallback) and format it."""
    table = _S[normalize_lang(lang)]
    text = table.get(key) or _S[DEFAULT_LANG][key]
    return text.format(**fmt) if fmt else text
