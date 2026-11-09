"""``hermes ussd ...`` commands and the ``/ussd`` slash command.

Every command prints to a stream it is given and returns an exit code, so tests drive them directly.
Nothing here waits for input unless stdin is a terminal: under cron or a pipe a missing value is an
error, never a hang (catalog rule 12).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional, TextIO

from .phone import mask, normalize_e164
from .settings import ConfigError, Settings, load_settings

# "cronjob" is on this list on purpose: Hermes runs cron jobs with the FULL toolset (terminal, file writes,
# code execution), so letting a phone user create jobs would bypass the `safe` pin entirely.
RISKY_TOOLSETS = ("terminal", "file", "code_execution", "hermes-cli", "all", "delegation", "browser", "cronjob")


def _env_getter() -> Callable[[str], Optional[str]]:
    from hermes_cli.config import get_env_value

    return get_env_value


def _save_env(name: str, value: str) -> None:
    from hermes_cli.config import save_env_value

    save_env_value(name, value)


def callback_urls(public_url: str, secret: str) -> dict[str, str]:
    base = public_url.rstrip("/")
    return {k: f"{base}/{secret}/{k}" for k in ("ussd", "sms", "dlr")}


def _https_public(url: str) -> Optional[str]:
    """The URL if it is a usable public https origin, else None."""
    p = urllib.parse.urlsplit(url or "")
    if p.scheme != "https" or not p.hostname or p.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):  # noqa: S104
        return None
    return f"https://{p.netloc}{p.path.rstrip('/')}"


# -- status ---------------------------------------------------------------------------------------

def snapshot_text(settings: Optional[Settings] = None, store: Any = None) -> str:
    from . import adapter

    own = store is None
    try:
        store = store or adapter.open_store()
        counts = store.status_counts()
        lines = ["hermes-ussd status"]
        if settings:
            lines.append(f"  mode: {'SANDBOX' if settings.sandbox else 'PRODUCTION'}; listener "
                         f"{settings.host}:{settings.port}; allowlist {len(settings.allowed)} number(s)"
                         f"{'; OPEN TO EVERYONE' if settings.allow_all else ''}")
            sent = store.counter(_today_key(settings))
            lines.append(f"  sent today: {sent}/{settings.daily_sms_cap}")
        lines.append("  outbox: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty"))
        bad = {k: v for k, v in counts.items() if k.startswith("failed") or k == "unknown"}
        if bad:
            lines.append("  attention: " + ", ".join(f"{k}={v}" for k, v in sorted(bad.items())))
        return "\n".join(lines)
    finally:
        if own and store is not None:
            store.close()


def _today_key(settings: Settings) -> str:
    from .store import day_key

    return day_key(time.time(), settings.day_offset_hours)


def ussd_command(raw_args: str = "") -> str:
    """``/ussd status`` inside a session."""
    if (raw_args or "").strip().lower() in ("", "status"):
        try:
            settings = load_settings(_env_getter())
        except ConfigError:
            settings = None
        return snapshot_text(settings)
    return "Usage: /ussd status"


# -- setup / rotate -------------------------------------------------------------------------------

def _prompt(label: str, *, secret: bool = False) -> str:
    return (getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")).strip()


def cmd_setup(args: argparse.Namespace, out: TextIO = sys.stdout, *, interactive: Optional[bool] = None,
              probe: Optional[Callable[[Settings], Any]] = None) -> int:
    tty = sys.stdin.isatty() if interactive is None else interactive
    username = args.username or (_prompt("Africa's Talking username (sandbox apps use 'sandbox')") if tty else "")
    api_key = args.api_key or (_prompt("API key", secret=True) if tty else "")
    if not username or not api_key:
        print("error: --username and --api-key are required when not run from a terminal", file=out)
        return 2
    env_name = args.env
    public = args.public_url or (_prompt("Public https URL of this machine (e.g. your tunnel; blank to skip)") if tty else "")
    if public and not _https_public(public):
        print("error: --public-url must be a public https URL (Africa's Talking will not call http or localhost)", file=out)
        return 2
    existing = _env_getter()("AT_WEBHOOK_SECRET")
    secret = existing if (existing and not args.regenerate) else secrets.token_urlsafe(32)
    try:
        settings = load_settings({"AT_USERNAME": username, "AT_API_KEY": api_key, "AT_WEBHOOK_SECRET": secret,
                                  "AT_ENV": env_name}.get)
    except ConfigError as exc:
        print(f"error: {exc}", file=out)
        return 2
    outcome = (probe or _probe)(settings)
    if outcome.kind == "auth":
        print("error: Africa's Talking rejected those credentials. Nothing was saved.", file=out)
        return 1
    if outcome.kind != "ok":
        print(f"warning: could not verify credentials ({outcome.detail}); saving anyway", file=out)
    for name, value in (("AT_USERNAME", username), ("AT_API_KEY", api_key), ("AT_ENV", env_name),
                        ("AT_WEBHOOK_SECRET", secret)):
        _save_env(name, value)
    if args.allow:
        numbers = [normalize_e164(n) for n in args.allow]
        bad = [n for n, ok in zip(args.allow, numbers, strict=True) if not ok]
        if bad:
            print(f"warning: ignored invalid number(s): {', '.join(bad)}", file=out)
        good = [n for n in numbers if n]
        if good:
            _save_env("AT_ALLOWED_USERS", ",".join(good))
    if args.ussd_code:
        _save_env("AT_USSD_CODE", args.ussd_code)
    print("Saved to your Hermes .env.", file=out)
    _print_next_steps(out, settings, public, secret, bool(args.allow))
    return 0


def _print_next_steps(out: TextIO, settings: Settings, public: str, secret: str, have_allow: bool) -> None:
    mode = ("SANDBOX (SMS go to the Africa's Talking simulator, not real phones)" if settings.sandbox
            else "PRODUCTION (real SMS, real cost)")
    print(f"\nMode: {mode}", file=out)
    base = _https_public(public) if public else None
    print("\nWebhook URLs to paste into the Africa's Talking dashboard (these contain a SECRET; treat them like a password):", file=out)
    if base:
        for kind, url in callback_urls(base, secret).items():
            print(f"  {kind:<5} {url}", file=out)
    else:
        print(f"  https://<your-public-host>/{secret}/ussd   (and /sms, /dlr)", file=out)
        print("  Re-run with --public-url https://<host> to print them in full.", file=out)
    print(f"\nThe listener binds {settings.host}:{settings.port}; point your tunnel/reverse proxy there.", file=out)
    if not have_allow and not _env_getter()("AT_ALLOWED_USERS"):
        print("\nNext: set who may use the service (everyone else is refused):", file=out)
        print("  hermes config set AT_ALLOWED_USERS +254700000001,+254700000002", file=out)
    print("\nThen: hermes plugins enable hermes-ussd && hermes gateway restart && hermes ussd doctor", file=out)


def cmd_rotate_secret(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    if not _env_getter()("AT_WEBHOOK_SECRET"):
        print("error: no AT_WEBHOOK_SECRET to rotate; run `hermes ussd setup` first", file=out)
        return 1
    secret = secrets.token_urlsafe(32)
    _save_env("AT_WEBHOOK_SECRET", secret)
    print("Secret rotated. The OLD webhook URLs stop working as soon as the gateway restarts.", file=out)
    public = _https_public(args.public_url or "")
    for kind, url in callback_urls(public or "https://<your-public-host>", secret).items():
        print(f"  {kind:<5} {url}", file=out)
    print("Update all three in the Africa's Talking dashboard, then: hermes gateway restart", file=out)
    return 0


# -- doctor ---------------------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str = ""


def _probe(settings: Settings):
    from . import adapter

    async def run():
        client = adapter.make_client(settings)
        try:
            return await client.probe_auth()
        finally:
            await client.aclose()

    return asyncio.run(run())


def cron_wrap() -> Optional[bool]:
    """Hermes's ``cron.wrap_response`` setting (True when cron output is wrapped), None if unreadable."""
    try:
        from hermes_cli.config import load_config

        return bool((load_config().get("cron") or {}).get("wrap_response", True))
    except Exception:  # noqa: BLE001 - advisory check only
        return None


def _http_post(url: str, form: dict[str, str], timeout: float = 5.0) -> tuple[int, str]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST",  # noqa: S310
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read(512).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(512).decode("utf-8", "replace")


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def run_doctor(public_url: str = "", *, probe: Optional[Callable] = None, store: Any = None,
               post: Callable = _http_post, listening: Callable = _listening) -> list[Check]:
    from . import adapter

    checks: list[Check] = []
    add = lambda n, s, d="": checks.append(Check(n, s, d))  # noqa: E731
    try:
        settings = load_settings(_env_getter())
    except ConfigError as exc:
        add("configuration", "fail", f"{exc}. Run `hermes ussd setup`.")
        return checks
    add("configuration", "ok", "required values present, webhook secret strong enough")
    add("mode", "warn" if settings.sandbox else "ok",
        "SANDBOX: SMS go to the Africa's Talking simulator, not real phones" if settings.sandbox
        else "PRODUCTION: real SMS, real cost")
    if settings.sandbox and settings.username != "sandbox":
        add("username/environment", "warn",
            "AT_ENV is sandbox but the username is not 'sandbox': sandbox apps are always named 'sandbox'; "
            "a live username will be rejected by the sandbox API")
    elif not settings.sandbox and settings.username == "sandbox":
        add("username/environment", "fail", "AT_ENV is production but the username is 'sandbox'")
    try:
        outcome = (probe or _probe)(settings)
        status = {"ok": "ok", "auth": "fail"}.get(outcome.kind, "warn")
        add("Africa's Talking credentials", status,
            "accepted" if status == "ok" else outcome.detail)
    except Exception as exc:  # noqa: BLE001
        add("Africa's Talking credentials", "warn", f"could not check: {type(exc).__name__}")
    own = store is None
    try:
        store = store or adapter.open_store()
        add("database", "ok", "opens, schema current")
        counts = store.status_counts()
        bad = {k: v for k, v in counts.items() if k.startswith("failed") or k == "unknown"}
        add("outbox", "warn" if bad else "ok",
            ("needs attention: " + ", ".join(f"{k}={v}" for k, v in sorted(bad.items()))) if bad else "no failures")
        sent = store.counter(_today_key(settings))
        add("daily SMS cap", "warn" if settings.daily_sms_cap and sent >= settings.daily_sms_cap else "ok",
            f"{sent}/{settings.daily_sms_cap} sent today")
    except Exception as exc:  # noqa: BLE001
        add("database", "fail", f"{type(exc).__name__}: {exc}")
    finally:
        if own and store is not None:
            store.close()
    if settings.allow_all:
        add("access", "warn", "OPEN TO EVERYONE (AT_ALLOW_ALL_USERS): anyone who finds the code can spend your SMS budget")
    elif settings.allowed:
        add("access", "ok", f"{len(settings.allowed)} allowlisted number(s)")
    else:
        add("access", "warn", "AT_ALLOWED_USERS is empty: every caller is refused until you set it")
    if settings.toolsets is None:
        add("toolsets", "warn", "AT_TOOLSETS=default: phone sessions get the gateway's full toolset")
    else:
        risky = [t for t in settings.toolsets if t in RISKY_TOOLSETS]
        add("toolsets", "fail" if risky else "ok",
            f"phone sessions may use {', '.join(settings.toolsets)}" + (f"; powerful: {', '.join(risky)}" if risky else ""))
    if cron_wrap() is True:
        add("cron wrapper", "warn", "Hermes adds a ~100-character header and footer to cron results, which eats SMS "
            "segments: run `hermes config set cron.wrap_response false` if you deliver cron output by SMS")
    local = f"http://{settings.host}:{settings.port}/{settings.webhook_secret}/ussd"
    if listening(settings.host, settings.port):
        add("listener", "ok", f"something is listening on {settings.host}:{settings.port}")
        add("callback self-test", *_selftest(post, local, settings))
    else:
        add("listener", "warn", f"nothing listening on {settings.host}:{settings.port} (start `hermes gateway`)")
    if public_url:
        base = _https_public(public_url)
        if not base:
            add("public URL", "fail", "must be a public https URL")
        else:
            add("public URL", *_selftest(post, f"{base}/{settings.webhook_secret}/ussd", settings))
    return checks


def _selftest(post: Callable, url: str, settings: Settings) -> tuple[str, str]:
    """A hop from a number nobody owns: must get a valid CON/END (proves wiring, sends nothing),
    and the same URL with a wrong secret must be a bare 404."""
    form = {"sessionId": "doctor", "phoneNumber": "+254000000000", "text": "", "serviceCode": "*0#", "networkCode": "0"}
    try:
        code, text = post(url, form)
        bad_url = url.replace(settings.webhook_secret, "x" * len(settings.webhook_secret))
        wrong_code, wrong_text = post(bad_url, form)
    except Exception as exc:  # noqa: BLE001
        return "warn", f"request failed: {type(exc).__name__}"
    if code != 200 or not text.startswith(("CON", "END")):
        return "fail", f"unexpected reply (HTTP {code})"
    if wrong_code != 404 or wrong_text:
        return "fail", "a wrong secret was not rejected with a bare 404"
    return "ok", "valid USSD reply; wrong secret rejected"


def cmd_doctor(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    checks = run_doctor(args.public_url or "")
    for c in checks:
        print(f"[{marks[c.status]}] {c.name}: {c.detail}", file=out)
    failed = [c for c in checks if c.status == "fail"]
    print(f"\n{len(failed)} failure(s), {sum(c.status == 'warn' for c in checks)} warning(s).", file=out)
    return 1 if failed else 0


# -- status / purge -------------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    try:
        settings = load_settings(_env_getter())
    except ConfigError:
        settings = None
    print(snapshot_text(settings), file=out)
    return 0


def cmd_purge(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    from . import adapter

    store = adapter.open_store()
    try:
        if args.number:
            n = normalize_e164(args.number)
            if not n:
                print("error: not a valid phone number", file=out)
                return 2
            removed = store.purge_number(n, include_optout=args.include_optout)
            print(f"Erased data for {mask(n)}: {removed}", file=out)
            if not args.include_optout:
                print("(its STOP record was kept so it is not texted again; add --include-optout to remove that too)", file=out)
        else:
            try:
                days = load_settings(_env_getter()).retention_days
            except ConfigError:
                days = 7
            print(f"Purged (older than {args.days or days} days): {store.purge(args.days or days)}", file=out)
        return 0
    finally:
        store.close()


# -- simulate: a phone in the terminal ------------------------------------------------------------

def cmd_simulate(args: argparse.Namespace, out: TextIO = sys.stdout, *, post: Callable = _http_post,
                 reader: Callable[[str], str] = input) -> int:
    try:
        settings = load_settings(_env_getter())
    except ConfigError as exc:
        print(f"error: {exc}", file=out)
        return 2
    number = normalize_e164(args.number)
    if not number:
        print("error: --number must be a phone number", file=out)
        return 2
    base = (args.url or f"http://{settings.host}:{settings.port}").rstrip("/") + f"/{settings.webhook_secret}"
    if args.sms:
        code, _ = post(f"{base}/sms", {"from": number, "to": "12345", "text": args.sms, "id": f"sim{secrets.token_hex(4)}",
                                       "date": time.strftime("%Y-%m-%d %H:%M:%S")})
        print(f"inbound SMS posted (HTTP {code})", file=out)
        return 0 if code == 200 else 1
    session = "sim" + secrets.token_hex(6)

    def hop(path: str) -> str:
        code, text = post(f"{base}/ussd", {"sessionId": session, "phoneNumber": number, "text": path,
                                           "serviceCode": args.code, "networkCode": "63902"})
        return text if code == 200 else f"[HTTP {code}: is the gateway running and the secret right?]"

    if args.path is not None:
        print(hop(args.path), file=out)
        return 0
    if reader is input and not sys.stdin.isatty():
        print("error: simulate needs a terminal (or pass --path / --sms)", file=out)
        return 2
    inputs: list[str] = []
    print(f"Dialing {args.code} as {number}  (type your choice, Ctrl-C to hang up)\n", file=out)
    while True:
        text = hop("*".join(inputs))
        verb, _, body = text.partition(" ")
        print("+" + "-" * 44 + f"+\n{body or text}\n+" + "-" * 44 + "+", file=out)
        if verb != "CON":
            if verb == "END":
                print("(session ended)", file=out)
            return 0
        try:
            inputs.append(reader("> "))
        except (EOFError, KeyboardInterrupt):
            print("\n(hung up)", file=out)
            return 0


# -- wiring ---------------------------------------------------------------------------------------

def build_parser(parser: Optional[argparse.ArgumentParser] = None) -> argparse.ArgumentParser:
    p = parser or argparse.ArgumentParser(prog="hermes ussd")
    sub = p.add_subparsers(dest="ussd_cmd")
    s = sub.add_parser("setup", help="validate credentials, create the webhook secret, print callback URLs")
    s.add_argument("--username")
    s.add_argument("--api-key", dest="api_key")
    s.add_argument("--env", choices=("sandbox", "production"), default="sandbox")
    s.add_argument("--public-url", dest="public_url", default="")
    s.add_argument("--allow", action="append", help="E.164 number allowed to use the service (repeatable)")
    s.add_argument("--ussd-code", dest="ussd_code", default="")
    s.add_argument("--regenerate", action="store_true", help="make a new webhook secret even if one exists")
    d = sub.add_parser("doctor", help="check configuration, credentials, listener and callbacks")
    d.add_argument("--public-url", dest="public_url", default="")
    sub.add_parser("status", help="outbox counts and daily SMS usage")
    r = sub.add_parser("rotate-secret", help="issue a new webhook secret (old URLs stop working)")
    r.add_argument("--public-url", dest="public_url", default="")
    pg = sub.add_parser("purge", help="apply retention now, or erase one number's data")
    pg.add_argument("--number")
    pg.add_argument("--include-optout", action="store_true")
    pg.add_argument("--days", type=int, default=0)
    m = sub.add_parser("simulate", help="dial the service from a pretend phone in this terminal")
    m.add_argument("--number", required=True)
    m.add_argument("--url", default="")
    m.add_argument("--code", default="*384*1#")
    m.add_argument("--path", default=None, help="send one hop (e.g. '1*hello') and print the screen")
    m.add_argument("--sms", default=None, help="post one inbound SMS instead of dialing")
    return p


_COMMANDS = {"setup": cmd_setup, "doctor": cmd_doctor, "status": cmd_status, "rotate-secret": cmd_rotate_secret,
             "purge": cmd_purge, "simulate": cmd_simulate}


def dispatch(args: argparse.Namespace) -> int:
    fn = _COMMANDS.get(getattr(args, "ussd_cmd", None) or "")
    if fn is None:
        build_parser().print_help()
        return 0
    return int(fn(args) or 0)


def register_cli(ctx) -> None:
    def handler(args: argparse.Namespace) -> None:
        code = dispatch(args)
        if code:
            raise SystemExit(code)

    ctx.register_cli_command(
        name="ussd", help="Manage the USSD & SMS gateway (Africa's Talking)",
        setup_fn=lambda parser: build_parser(parser), handler_fn=handler,
        description="Set up, check and test the hermes-ussd platform.")
