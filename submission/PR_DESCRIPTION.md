# Add hermes-ussd (USSD & SMS gateway platform, Africa's Talking)

Adds `plugin-catalog/hermes-ussd.yaml`. Submitted by the repository owner (rule 5), for the Hermes Agent Plugin Hackathon.

## What it is

A `kind: platform` plugin that lets any phone use Hermes: dial a USSD code (instant, deterministic menu), ask a question, receive the agent's answer by SMS; long answers can be read back on USSD option 2. Built on Africa's Talking, so it reaches feature phones with no data connection (Kenya, Uganda, Tanzania, Rwanda, Nigeria, Malawi and more).

Public surfaces used: `ctx.register_platform`, `ctx.register_cli_command`, `ctx.register_command`, `ctx.register_skill`. No tools, hooks or middleware are registered (so no agent-callable send tool exists). No core overrides (rule 9).

## Disclosures (rule 13)

- **Network:** outbound HTTPS to `api.africastalking.com` / `api.sandbox.africastalking.com` only (SMS API), through Hermes's SSRF-guarded client plus a two-host allowlist; redirects not followed. No telemetry.
- **Listener:** an HTTP server on `127.0.0.1:8790` (configurable) that the user exposes through their own tunnel or reverse proxy. Africa's Talking callbacks are **unsigned**, so authenticity rests on a secret URL path (constant-time compare, bare 404 for everything else, rotation via `hermes ussd rotate-secret`). Access is closed by default: only numbers in `AT_ALLOWED_USERS` can start an agent turn.
- **Storage:** phone numbers, question and reply text, outbox state in a SQLite file in Hermes plugin storage (mode 0600), 7-day default retention, `hermes ussd purge`, opt-outs kept.
- **Credentials:** read through Hermes (`get_scoped_secret`); `hermes ussd setup` writes them with Hermes's own config helper. The plugin never reads `.env`, `auth.json` or any other credential store.
- **Background work:** an outbox worker and an hourly purge task inside the gateway process. No subprocesses, no shell commands, no self-update, no binaries, no bytecode.
- **Cost:** sending SMS costs money on the user's Africa's Talking account; a daily cap (default 200) and segment cap are on by default.
- **Agent exposure:** phone sessions are pinned to the `safe` toolset (`AT_TOOLSETS`); `hermes ussd doctor` fails on powerful toolsets (including `cronjob`, since cron jobs run with the full toolset). The plugin never sets `trusted_inbound`, never bypasses approvals, and USSD text never carries gateway-command authority.

## Verification

- `hermes plugins validate <path> --install-deps` and `hermes plugins doctor <path> --ci` pass (CI log: <LINK>).
- Test suite (unit, Hypothesis fuzzing, integration against the real `PlatformEntry`/`BasePlatformAdapter`/plugin storage, chaos, security): 298 tests passing in CI on Python 3.14 against Hermes `cacad951` (pure core also on 3.11 to 3.13).
- Chaos: 1,000 turns with 20% provider failures delivered once each with none lost; 10% duplicate callbacks produced no duplicate turns or SMS; ambiguous network failures are never resent; outage, balance exhaustion and hostile traffic covered.
- Mutation check (`tools/mutation_check.py`): removing any of eight key controls fails the suite.
- Run against a real Hermes gateway with a stubbed model endpoint: USSD ask, `safe`-toolset-only tool list, SMS outbox, USSD read-back with paging, and operator cron delivery.
- Africa's Talking sandbox end-to-end: <DATE, two clean runs, link to recording>.

## Compatibility

`requires_hermes: ">=0.22.5"` in both `plugin.yaml` and the catalog entry (rule 14). Verified on Hermes `main` at `cacad951`; the floor is a conservative one taken from current catalog practice and has not been run on 0.22.5 itself. Python dependency: `aiohttp>=3.9,<4` (rule 10; CI installs with a 3-day release-age quarantine).

## Known limitations

See the README "Verification status" and SECURITY.md "Known limitations": one listener per account and port; Hermes core logs the chat id (the phone number) like it does for its other phone adapters; concurrent questions from one number follow Hermes's busy-session policy; the Kiswahili strings need native review.

Demo (under 90 s): <LINK>. The demo runs on the Africa's Talking sandbox; a live USSD code needs operator approval.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01HL8c88Mqqk7EvGRRcFCrsb
