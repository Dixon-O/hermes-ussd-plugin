# Security

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this repository (Security tab, "Report a vulnerability"). Do not open a public issue for anything that exposes a secret, lets a stranger spend an operator's SMS balance, or reaches the agent without being allowlisted. Expect an acknowledgement within a few days.

## What this plugin exposes, in one paragraph

`hermes-ussd` opens one HTTP listener (default `127.0.0.1:8790`, loopback only) that Africa's Talking calls for USSD hops, inbound SMS and delivery reports, and makes one kind of outbound call: `POST` to the Africa's Talking SMS API (`api.africastalking.com` or `api.sandbox.africastalking.com`, nothing else). It registers **no agent-callable tools**, so a prompt-injected agent cannot send SMS, read the store or reach any number through this plugin.

## The one thing to understand: callbacks are unsigned

Africa's Talking does not sign its USSD/SMS callbacks. Anyone who learns the callback URL can post to it. The only authentication is therefore **a secret path segment** (`/<AT_WEBHOOK_SECRET>/ussd`), compared in constant time. Treat the callback URLs like passwords:

* Do not paste them into issues, chats or screenshots.
* Prefer a tunnel or reverse proxy you control (a named tunnel rather than a throwaway URL).
* If a URL may have leaked, run `hermes ussd rotate-secret`, update the three URLs in the Africa's Talking dashboard, and restart the gateway. The old URLs stop working at restart.

Even with a leaked URL an attacker gains much less than it sounds, because **access is closed by default**: a request only reaches the agent when the phone number is in `AT_ALLOWED_USERS` (or `AT_ALLOW_ALL_USERS=true`, which `hermes ussd doctor` flags in red). But an attacker who knows a leaked URL and an allowlisted number can *claim* to be that number, because the number is a field in the request. SMS identity is weak by nature (SIM swap and operator spoofing are out of any plugin's control). That is why the second line of defence is real, not decorative:

* Phone sessions run on the `safe` toolset only (`AT_TOOLSETS`, default `safe`). `hermes ussd doctor` fails if you configure powerful toolsets (`terminal`, `file`, `code_execution`, `browser`, `cronjob`, ...). Note that `cronjob` counts: Hermes runs cron jobs with its full toolset, so a phone user who could create one would escape the pin. Enabling any of them is your explicit decision.
* USSD text never carries gateway-command authority. Inbound SMS is plain text unless you set `AT_ALLOW_COMMANDS=true`.
* The plugin never sets `trusted_inbound` and never bypasses Hermes approvals.

## Threat model and where each control is tested

| ID | Threat | Control | Test |
|----|--------|---------|------|
| T1 | Forged or guessed webhook | Secret path, `hmac.compare_digest`, bare identical 404 for everything else (wrong secret, wrong method, wrong content type, oversize, catch-all) | `test_webhooks.py` (`wrong_secret`, `every_rejection_is_byte_identical`, `constant_time`) |
| T2 | Agent tools abused through a spoofed number | `safe` toolset pin, fail-closed `toolsets_for_source`, no `trusted_inbound`, doctor fails on powerful toolsets | `test_security.py::phone_users_get_a_restricted_toolset`, `test_cli_sender.py::doctor_flags_misconfiguration` |
| T3 | Prompt injection in SMS/USSD text | Text is an ordinary user turn; no agent-callable send tool; replies go only to the sender; commands disabled by default | `test_security.py::slash_commands_over_sms_are_inert_by_default`, `no_agent_callable_surface_is_registered` |
| T4 | SMS pumping / toll fraud | Unknown numbers get no SMS and no agent turn; per-number rate limits; persisted daily SMS cap; segment cap; STOP list; outbound targets restricted to the allowlist/home number (also for cron and `hermes send`) | `test_service.py`, `test_outbox.py` (cap), `test_cli_sender.py::validate_target_*` |
| T5 | Flooding / resource exhaustion | 4 KB body limit, 2 s read timeout, 6 s USSD deadline, per-number token buckets, bounded retry schedule | `test_webhooks.py`, `test_chaos.py::hostile_traffic_does_not_disturb_real_users` |
| T6 | Replay / duplicate delivery | Durable `inbound_seen` keyed on SMS id or session+path hash; idempotent outbox | `test_chaos.py::duplicate_callbacks_never_cause_duplicate_sms`, `test_security.py::replaying_an_old_sms_id...` |
| T7 | Hostile input (control chars, huge text, odd Unicode, CON/END smuggling) | Field length caps; the plugin, not the model, writes the `CON`/`END` prefix; screens validated before sending; Hypothesis fuzzing of the menu | `test_menu.py`, `test_security.py::hostile_ussd_text_never_breaks_the_service` |
| T8 | Approval hijack over SMS | **Not applicable by design**: this plugin does not implement SMS approval codes. Hermes approvals stay in Hermes; with the `safe` toolset there is nothing to approve | n/a |
| T9 | Secret leakage | Secrets read per use through Hermes (`get_scoped_secret`); never stored in the DB; host allowlist for the API key; no redirects followed; access log disabled so the secret path never reaches a log line | `test_security.py::no_secret_key_number_or_text_in_logs...`, `test_client.py::redirect_is_not_followed` |
| T10 | Personal data at rest | Database in plugin storage with mode `0600`/`0700`; default 7-day retention of message text; `hermes ussd purge --number` erases one person; opt-outs deliberately kept so they are not texted again | `test_security.py::database_is_private_to_the_owner`, `stop_is_honoured_immediately_and_survives_purge`, `test_cli_sender.py::purge_*` |
| T11 | Live account used by accident | `AT_ENV` defaults to `sandbox`; doctor warns in sandbox, flags username/environment mismatch | `test_cli_sender.py::doctor_flags_username_environment_mismatch` |
| T12 | Two gateways/profiles on one account | Platform lock scoped to `username:port` | `test_security.py::second_gateway_on_the_same_account_and_port_is_refused` |
| T13 | SSRF through the HTTP client | Hermes's SSRF-guarded client plus a two-host allowlist enforced before every request | `test_client.py::host_allowlist_enforced` |
| T14 | Supply chain | One runtime dependency (`aiohttp>=3.9,<4`); no binaries, no bytecode, no self-update; CI installs with a 3-day release-age quarantine | `hermes plugins validate --install-deps` (security scan) in CI |
| T15 | Texting people who opted out | `STOP`/`START`/`HELP` handled before the agent; opt-out checked before every send, including cron and `hermes send` | `test_outbox.py::opted_out_recipient...`, `test_cli_sender.py::standalone_send_honours_opt_out` |

`tools/mutation_check.py` removes eight of these controls one at a time (allowlist, secret check, ambiguous-failure retry guard, replay guard, daily cap, opt-out, outbound-target restriction, USSD command authority) and fails if the test suite still passes.

## Delivery guarantees (reliability is part of security here)

* **Store first, send second.** An answer is written to the database before any SMS is attempted, so it can always be read back on USSD option 2.
* **At most once.** A crash mid-send or an ambiguous network failure marks the message `unknown` and it is never resent: a duplicate text costs a person money and trust, a missing one is recoverable on USSD.
* **Provider back-pressure.** Rejected credentials or an empty balance pause sending (5 and 15 minutes) and keep messages queued instead of hammering the API.

## Data inventory

Stored locally in `<HERMES_HOME>/plugin-data/...` (Hermes plugin storage): phone numbers, question and answer text (purged after `AT_RETENTION_DAYS`, default 7), outbox status and Africa's Talking message ids and costs, opt-outs, language preference, counters. Credentials are stored only where Hermes stores them (your Hermes `.env`). Nothing is sent anywhere except the SMS API call to Africa's Talking, and the text of agent turns goes wherever your configured Hermes model sends it.

## Known limitations (stated, not hidden)

* **Hermes core logs the chat id.** The phone number is the Hermes chat id, and Hermes core's gateway logs it when it sends a reply (e.g. `Sending response (N chars) to +254...`), exactly as for its Signal/SMS/WhatsApp adapters. The plugin's own log lines mask numbers and never contain message text or secrets (tested), but protect your gateway logs like any other file that contains phone numbers.
* **Concurrent questions from one number.** If the same number asks a second question while the first is still being answered, Hermes's busy-session policy (`interrupt` or `queue`, configured in Hermes) decides what happens. The plugin never loses either answer, but the `Ref` shown on the SMS may refer to the earlier question.
* **Africa's Talking status codes.** The mapping of provider status codes to retry/permanent/balance is written from the documentation and marked for sandbox verification (see README "Verification status").
* **No global cap on in-flight agent turns** beyond per-number limits and Hermes's own concurrency handling. Do not use `AT_ALLOW_ALL_USERS` on a public code without a tight `AT_DAILY_SMS_CAP`.
* **Plugin code runs in the gateway process** with the user's permissions, like every Hermes plugin. The small codebase and the catalog review are the mitigation.
