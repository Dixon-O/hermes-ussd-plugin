# hermes-ussd

**Hermes for every phone.** Dial a USSD code on any handset, including a feature phone with no data plan. A menu answers instantly, you type a question, and the Hermes agent's answer arrives by SMS. Long answers can be read back page by page on the menu. Built on [Africa's Talking](https://africastalking.com) (Kenya, Uganda, Tanzania, Rwanda, Nigeria, Malawi and more), as a Hermes `kind: platform` plugin.

```
*384*1234#                      CON Hermes                       <- instant, from local code
                                1 Ask a question  2 Read last answer  3 Status ...
1 -> "Maize leaves yellow       END Got it. Your answer will come by SMS. Ref 4821.
      with streaks, Kisii"                                       <- session closes at once
                                (agent works...)
SMS:  Ref 4821: Streaking points to maize streak virus... Full answer: dial *384*1234# option 2
```

USSD cannot wait for a language model: the operator drops the session after about ten seconds. So only option 1 starts an agent turn, the acknowledgement ends the session immediately, and everything else (menu, paging, status, language) is answered by deterministic local code. The agent's answer is **stored first, texted second**, so it can always be read back on option 2 even if the SMS never arrives.

## Try it in two minutes

**Easiest path (a pretend phone in your terminal, nothing to configure): [TRY_IT.md](TRY_IT.md).** Three lines: `git clone https://github.com/Dixon-O/hermes-ussd`, `cd hermes-ussd`, `bash try.sh`.

## Manual version (no Africa's Talking account needed for the USSD half)

```bash
hermes plugins install Dixon-O/hermes-ussd          # or: hermes plugins install hermes-ussd  (once listed)
hermes plugins enable hermes-ussd

# Placeholder credentials are enough to exercise the menu, the agent and USSD read-back.
hermes config set AT_USERNAME sandbox
hermes config set AT_API_KEY dummy-key-for-local-testing
hermes config set AT_WEBHOOK_SECRET "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
hermes config set AT_ALLOWED_USERS +254700000001

hermes gateway run &                                 # starts the listener on 127.0.0.1:8790
hermes ussd simulate --number +254700000001          # a pretend phone in your terminal
```

Dial, pick `1`, ask something, hang up, dial again and pick `2`: the answer is there. SMS delivery needs real sandbox credentials (next section); with placeholders the SMS is held and `hermes ussd status` shows why, which is itself a demonstration of the "nothing is lost" design.

## Setting up with the Africa's Talking sandbox (free)

1. Create an account and open the **Sandbox** app. Copy its API key (the sandbox username is always `sandbox`).
2. In the sandbox, create a **USSD service code** and an **SMS shortcode**. Paste the three callback URLs printed in step 4.
3. Expose the listener over HTTPS with a tunnel or reverse proxy you control (a named tunnel is better than a throwaway URL, because the URL is the credential).
4. Run setup. It checks the credentials with a harmless call, generates a strong webhook secret, saves everything through Hermes, and prints the callback URLs:

   ```bash
   hermes ussd setup --username sandbox --api-key <KEY> --public-url https://<your-host> \
       --allow +254700000001 --ussd-code '*384*1234#'
   hermes gateway restart
   hermes ussd doctor --public-url https://<your-host>
   ```

5. Open the sandbox **phone simulator**, dial your code, and ask.

`hermes ussd doctor` checks configuration, credentials, database, outbox, daily cap, access policy, toolsets, that the listener answers, and (with `--public-url`) that the public URL answers correctly and rejects a wrong secret with a bare 404.

## What it does

| Surface | Behaviour |
|---|---|
| USSD menu | 1 Ask, 2 Read last answer (paged, `98` = more), 3 Status, 4 Language (English, Kiswahili), 5 Help. `0` back, `00` main menu. Every reply is validated to fit the 182-character limit. |
| Inbound SMS | Texting the shortcode asks the agent; the answer comes back by SMS. `STOP` opts out, `START` opts back in, `HELP` replies with usage. |
| SMS replies | Markdown stripped, GSM-7 normalised, split into at most 3 segments (`AT_SMS_MAX_SEGMENTS`); if truncated, the last segment says to dial the code and choose 2, where the full answer lives. |
| Proactive SMS | `hermes cron create --deliver africastalking:+254700000001 ...` and `hermes send --to africastalking:+254700000001 ...` use the same outbox, caps and opt-outs. Only allowlisted or home-channel numbers are accepted as targets. For SMS, run `hermes config set cron.wrap_response false` so Hermes does not add its header and footer (`doctor` reminds you). |
| Slash command | `/ussd status` inside a Hermes session. |
| CLI | `hermes ussd setup | doctor | status | rotate-secret | purge | simulate` |

Three scenarios this is built for: a **farmer** asking about a crop problem and getting a sourced answer on a basic phone; a **shop owner or clinic** getting a scheduled morning digest by SMS (an operator-created cron job; phone users cannot schedule jobs unless you deliberately allow it); a **developer without data** asking "is staging up?" (this needs the operator to deliberately enable more powerful toolsets, see Security).

## Configuration

Only the first three are required. Set with `hermes config set NAME value` (or let `hermes ussd setup` do it).

| Variable | Default | Meaning |
|---|---|---|
| `AT_USERNAME` | | Africa's Talking username (`sandbox` for the sandbox). |
| `AT_API_KEY` | | API key (secret). |
| `AT_WEBHOOK_SECRET` | | 24-128 chars of `A-Z a-z 0-9 _ -`; the secret path segment. Generated by `hermes ussd setup`. |
| `AT_ALLOWED_USERS` | empty = nobody | Comma-separated numbers (E.164 or national format) allowed to use the agent. |
| `AT_ALLOW_ALL_USERS` | `false` | Open to anyone who finds the code (dev only; doctor warns). |
| `AT_ENV` | `sandbox` | `sandbox` or `production`. |
| `AT_USSD_CODE`, `AT_SMS_SENDER` | | Display only / registered sender id. |
| `AT_HOME_CHANNEL` | | Number for cron/notification delivery. |
| `AT_WEBHOOK_HOST`, `AT_WEBHOOK_PORT` | `127.0.0.1`, `8790` | Listener bind address. Keep loopback and put a tunnel or proxy in front. |
| `AT_DEFAULT_COUNTRY_CODE` | `254` | Calling code for numbers written without `+`. |
| `AT_LANGUAGE` | `en` | Default menu language (`en`, `sw`). |
| `AT_TOOLSETS` | `safe` | Hermes toolsets exposed to phone users. |
| `AT_ALLOW_COMMANDS` | `false` | Let inbound SMS act as gateway slash-commands (never from USSD). |
| `AT_DAILY_SMS_CAP` | `200` | Maximum SMS per day (day boundary at `AT_DAY_OFFSET_HOURS`, default UTC+3). `0` disables sending. |
| `AT_SMS_MAX_SEGMENTS` | `3` | Segments per answer. |
| `AT_ASKS_PER_HOUR`, `AT_HOPS_PER_MINUTE` | `10`, `30` | Per-number rate limits. |
| `AT_RETENTION_DAYS` | `7` | Days message text is kept. Opt-outs are never purged. |

## Security in brief

* **Closed by default.** No allowlist means every caller is refused; a stranger can never start an agent turn or cost you an SMS.
* **Callbacks are unsigned by Africa's Talking**, so the secret URL path is the only authentication. Treat it like a password and rotate with `hermes ussd rotate-secret`.
* **Phone users get the `safe` toolset** (web, vision, image generation) unless you choose otherwise; `doctor` fails on powerful toolsets, including `cronjob` (Hermes runs cron jobs with its full toolset, so letting a phone user create one would bypass the pin). The plugin registers no agent-callable tools, never bypasses Hermes approvals and never sets `trusted_inbound`.
* **Cost controls**: daily SMS cap, segment cap, per-number limits, outbound targets restricted to the allowlist.
* **Privacy**: plugin logs mask numbers and never contain message text; the database is `0600`; 7-day retention; `hermes ussd purge --number <n>` erases one person.

Full threat model with the test for each control, residual risks and known limitations: [SECURITY.md](SECURITY.md).

## Data notice

This plugin stores phone numbers and the text of questions and answers in its Hermes plugin database for `AT_RETENTION_DAYS` days so answers can be read back. Tell your users. SMS and USSD traffic also passes through Africa's Talking and the mobile operators under their terms.

## Costs

Africa's Talking charges per SMS segment (and for a USSD code and shortcode in production). See their pricing for your country. The sandbox is free. `AT_DAILY_SMS_CAP` bounds your spend, and `hermes ussd status` shows today's usage.

## Disclosures (catalog rule 13)

* **Network:** outbound HTTPS to `api.africastalking.com` / `api.sandbox.africastalking.com` only (SMS API). No telemetry.
* **Listener:** one HTTP server on `127.0.0.1:8790` (configurable) that you expose through your own tunnel/proxy.
* **Storage:** a SQLite file in Hermes plugin storage (numbers, reply text, outbox state), mode `0600`.
* **Credentials:** read through Hermes (`get_scoped_secret`); `hermes ussd setup` writes them with Hermes's own config helper.
* **Background work:** an outbox worker and an hourly purge task inside the gateway process; no other processes, no shell commands, no self-update, no binaries.

## Design notes

* **Store first, send second; at most once.** A crash or ambiguous network failure marks a message `unknown` and it is never resent (a duplicate SMS costs the recipient money). Connection failures retry at roughly 2, 8 and 30 seconds; rejected credentials or an empty balance pause sending and keep messages queued.
* **Pure core.** The menu state machine, GSM-7 splitting, store, outbox and service have no Hermes imports and are tested directly; only `adapter.py`, `sender.py` and `cli.py` touch Hermes.
* **Concurrent asks from one number** follow Hermes's busy-session policy; no answer is lost, but the `Ref` on an SMS may name the earlier question.

## Verification status

What is verified, and what is not, as of this commit:

| Item | Status |
|---|---|
| Unit, property-based (Hypothesis), integration (real Hermes `PlatformEntry`, `BasePlatformAdapter`, plugin storage), chaos and security suites | Passing locally on Python 3.14 against Hermes `main` @ `cacad951`; CI runs the same plus a pure-core job on 3.11 to 3.13 |
| Chaos: 1,000 turns with 20% provider failures | Every reply delivered once, none lost, none duplicated; also 10% duplicate callbacks, ambiguous network drops (never resent), total outage then recovery, balance exhaustion (paused, nothing dropped), hostile traffic |
| `hermes plugins validate` and `hermes plugins doctor --ci` | Pass locally (`--install-deps` is run in CI) |
| Mutation check (`tools/mutation_check.py`) | 8 of 8 removed controls caught by the tests |
| Africa's Talking **sandbox** E2E (Nov 11 2026, Hermes v0.23.1 stable, Gemini model, tunnel to a laptop) | **Verified live:** USSD menu, ask, `END` acknowledgement, agent answer by SMS (1 to 3 segments, KES 0.80 per segment, message id and cost returned by the SMS API), read-back and paging on option 2, Status, language switch, allowlist refusal. **Sandbox quirk:** the simulator posts delivery reports as `Failed` / `DeliveryFailure` for messages it did display, so delivery status is not meaningful in the sandbox; it is recorded as reported. Production delivery-report behaviour is **not verified** |
| `requires_hermes` floor | `>=0.22.5` is a conservative floor; tested on `main` @ `cacad951` (CI) and, by hand end to end, on stable v0.23.1 |
| Kiswahili strings | First pass; needs review by a native speaker before production use |
| Not supported | Shared-ingress / multiplexed profiles (one listener per account and port; a second gateway is refused), SMS approval codes, per-message delivery status on the USSD Status screen (delivery reports are recorded in the outbox; the screen shows counts), voice, media |

## Roadmap

Additional operators/aggregators behind the same adapter, an optional deterministic ledger toolset for shop-keeping (record sales and debts, scheduled summaries) so small businesses can keep books from a feature phone without enabling general tools, signed-callback support if providers add it, more languages.

## Development

```bash
python -I -m pytest -q          # unit + integration + chaos + security (Hermes-dependent tests skip without Hermes)
ruff check .
python tools/mutation_check.py  # remove each key control, expect the suite to fail
```

MIT licensed. Built in Kisumu.
