<div align="center">

# hermes-ussd

### Hermes for every phone

Dial a USSD code on **any** handset, including a feature phone with no data plan.
A menu answers instantly, you type a question, and the Hermes agent's answer arrives by SMS.

[![CI](https://github.com/Dixon-O/hermes-ussd-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/Dixon-O/hermes-ussd-plugin/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20to%203.14-3776AB?logo=python&logoColor=white)
![Hermes](https://img.shields.io/badge/Hermes-%3E%3D0.22.5-F5A524)
![License](https://img.shields.io/badge/license-MIT-2EA44F)
![Tests](https://img.shields.io/badge/tests-300%20passing-2EA44F)

<br>

[![Watch the demo](https://img.shields.io/badge/%E2%96%B6%EF%B8%8F_Watch_the_80s_demo-F5A524?style=for-the-badge&labelColor=0B0E14)](https://x.com/jmsonrich/status/2120585302523744356)
[![Try it in 2 minutes](https://img.shields.io/badge/%F0%9F%93%9E_Try_it_in_2_minutes-2EA44F?style=for-the-badge&labelColor=0B0E14)](#try-it-in-two-minutes)
[![Security](https://img.shields.io/badge/%F0%9F%94%92_Security-SECURITY.md-1F6FEB?style=for-the-badge&labelColor=0B0E14)](SECURITY.md)

</div>

---

## Quick links

| I want to... | Go to |
|---|---|
| See it work | [Watch the demo](https://x.com/jmsonrich/status/2120585302523744356) |
| Try it on my computer | [Try it in two minutes](#try-it-in-two-minutes) |
| Connect a real phone number | [Set up the Africa's Talking sandbox](#set-up-with-the-africas-talking-sandbox-free) |
| Check it is safe to run | [Security in brief](#security-in-brief) |
| Look up a setting | [Configuration](#configuration) |

---

## How it works

Africa's Talking (AT) handles the phone network. This plugin is the bridge between an AT USSD/SMS account and the Hermes agent.

```
  feature phone                 Africa's Talking                 your machine
  ─────────────                 ────────────────                 ────────────
  dial *384*1234#  ──USSD──►    callback  ──HTTPS──►  tunnel ──►  hermes-ussd
  pick 1, ask a question                                           (local menu, instant)
                                                                   │
                                                                   ▼
  SMS answer      ◄──SMS────    send API  ◄──HTTPS──────────────  Hermes agent (LLM)
  "Ref 4821: ..."                                                 answer stored FIRST
```

> [!IMPORTANT]
> USSD cannot wait for a language model: the operator drops the session after about ten seconds.
> So only option 1 starts an agent turn, the acknowledgement ends the session immediately, and everything else
> (menu, paging, status, language) is answered by local code. The answer is **stored first, texted second**,
> so it can always be read back on option 2 even if the SMS never arrives.

---

## Try it in two minutes

No phone, no Africa's Talking account, no tunnel. You get a **pretend phone inside your terminal**.

> [!NOTE]
> You need Hermes installed with a model chosen. Windows users: run this inside WSL (Ubuntu).
> Not installed yet? Run these once:
>
> ```bash
> curl -fsSLO https://hermes-agent.nousresearch.com/install.sh
> bash install.sh
> hermes model        # pick any model, paste its key
> ```

**Then one command block:**

```bash
git clone https://github.com/Dixon-O/hermes-ussd-plugin
cd hermes-ussd-plugin
bash try.sh
```

It installs the plugin, starts Hermes, and shows the phone menu:

```
+--------------------------------------------+
  Hermes
  1 Ask a question
  2 Read last answer
  3 Status
  4 Language
  5 Help
+--------------------------------------------+
>
```

| Step | Type this | You should see |
|---|---|---|
| 1 | `1` then Enter | "Ask a question" |
| 2 | `Maize leaves yellow with streaks, Kisii` then Enter | "Got it. Your answer will come by SMS. Ref 1234." (session ends, as on a real phone) |
| 3 | `Y` when asked to dial again, then `2` | Your stored answer, on the menu |

> [!TIP]
> Everything is local. The fake SMS credentials contact nothing, and only the test number `+254700000001` is allowed.
> The service stays closed to everyone else by default.

**Not shown here:** the real text message arriving on a phone. That needs an Africa's Talking account. Watch the [demo](https://x.com/jmsonrich/status/2120585302523744356), or follow [the sandbox setup](#set-up-with-the-africas-talking-sandbox-free) (free, about 15 minutes).

<details>
<summary><b>Something went wrong?</b></summary>

- **"Sorry, I could not answer that just now":** Hermes has no working model. Run `hermes model`, then try again.
- **Hermes did not start:** the script prints the log. Another Hermes gateway may be using port 8790; run `TRY_PORT=8791 bash try.sh`.
- **Undo everything:** `hermes plugins disable hermes-ussd`.
- **Full write-up of the same steps:** [TRY_IT.md](TRY_IT.md).

</details>

---

## Set up with the Africa's Talking sandbox (free)

1. Create an account and open the **Sandbox** app. Copy its API key. The sandbox username is always `sandbox`.
2. In the sandbox, create a **USSD service code** and an **SMS shortcode**. You will paste three callback URLs here in step 4.
3. Expose the listener over HTTPS with a tunnel or reverse proxy you control. A named tunnel is better than a throwaway URL, because the URL is the credential.
4. Run setup. It checks the credentials with a harmless call, generates a strong webhook secret, saves everything through Hermes, and prints the callback URLs:

   ```bash
   hermes ussd setup --username sandbox --api-key <KEY> --public-url https://<your-host> \
       --allow +254700000001 --ussd-code '*384*1234#'
   hermes gateway restart
   hermes ussd doctor --public-url https://<your-host>
   ```

5. Open the sandbox **phone simulator**, dial your code, and ask.

> [!TIP]
> `hermes ussd doctor` checks configuration, credentials, database, outbox, daily cap, access policy, toolsets, that the listener answers, and (with `--public-url`) that the public URL answers and rejects a wrong secret with a bare 404.

---

## What it does

| Surface | Behaviour |
|---|---|
| **USSD menu** | 1 Ask, 2 Read last answer (paged, `98` = more), 3 Status, 4 Language (English, Kiswahili), 5 Help. `0` back, `00` main menu. Every reply fits the 182-character limit. |
| **Inbound SMS** | Texting the shortcode asks the agent; the answer comes back by SMS. `STOP` opts out, `START` opts back in, `HELP` replies with usage. |
| **SMS replies** | Markdown stripped, GSM-7 normalised, split into at most 3 segments (`AT_SMS_MAX_SEGMENTS`). If truncated, the last segment says to dial the code and choose 2, where the full answer lives. |
| **Proactive SMS** | `hermes cron create --deliver africastalking:+254700000001 ...` and `hermes send --to africastalking:+254700000001 ...` share the outbox, caps and opt-outs. Only allowlisted or home-channel numbers are valid targets. |
| **Slash command** | `/ussd status` inside a Hermes session. |
| **CLI** | `hermes ussd setup \| doctor \| status \| rotate-secret \| purge \| simulate` |

**Built for:**
- 🌾 **A farmer** asking about a crop problem and getting a sourced answer on a basic phone.
- 🏪 **A shop owner or clinic** getting a scheduled morning digest by SMS (an operator-created cron job).
- 📶 **Someone with no data** asking "is staging up?" (needs the operator to deliberately enable more toolsets; see Security).

---

## Security in brief

> [!CAUTION]
> The phone listener is reachable from the internet once you expose it. Read these four rules before you set it up.

- 🔒 **Closed by default.** No allowlist means every caller is refused. A stranger can never start an agent turn or cost you an SMS.
- 🔑 **The callback path is the password.** Africa's Talking does not sign callbacks, so the secret URL path is the only authentication. Rotate it with `hermes ussd rotate-secret`.
- 🧰 **Phone users get the `safe` toolset** (web, vision, image generation). `doctor` fails on powerful toolsets, including `cronjob`, because cron jobs run with the full toolset. The plugin registers no agent-callable tools and never sets `trusted_inbound`.
- 💸 **Cost controls.** Daily SMS cap, segment cap, per-number rate limits, and outbound targets restricted to the allowlist.

Full threat model, the test for each control, residual risks and known limitations: **[SECURITY.md](SECURITY.md)**.

---

## Configuration

Only the first three are required. Set them with `hermes config set NAME value`, or let `hermes ussd setup` do it.

<details>
<summary><b>All settings (click to expand)</b></summary>

| Variable | Default | Meaning |
|---|---|---|
| `AT_USERNAME` | | Africa's Talking username (`sandbox` for the sandbox). |
| `AT_API_KEY` | | API key (secret). |
| `AT_WEBHOOK_SECRET` | | 24-128 chars of `A-Z a-z 0-9 _ -`; the secret path segment. Generated by `hermes ussd setup`. |
| `AT_ALLOWED_USERS` | empty = nobody | Comma-separated numbers (E.164 or national format) allowed to use the agent. |
| `AT_ALLOW_ALL_USERS` | `false` | Open to anyone who finds the code. Dev only; `doctor` warns. |
| `AT_ENV` | `sandbox` | `sandbox` or `production`. |
| `AT_USSD_CODE`, `AT_SMS_SENDER` | | Display only / registered sender id. |
| `AT_HOME_CHANNEL` | | Number for cron and notification delivery. |
| `AT_WEBHOOK_HOST`, `AT_WEBHOOK_PORT` | `127.0.0.1`, `8790` | Listener bind address. Keep loopback and put a tunnel or proxy in front. |
| `AT_DEFAULT_COUNTRY_CODE` | `254` | Calling code for numbers written without `+`. |
| `AT_LANGUAGE` | `en` | Default menu language (`en`, `sw`). |
| `AT_TOOLSETS` | `safe` | Hermes toolsets exposed to phone users. |
| `AT_ALLOW_COMMANDS` | `false` | Let inbound SMS act as gateway slash-commands (never from USSD). |
| `AT_DAILY_SMS_CAP` | `200` | Maximum SMS per day (day boundary at `AT_DAY_OFFSET_HOURS`, default UTC+3). `0` disables sending. |
| `AT_SMS_MAX_SEGMENTS` | `3` | Segments per answer. |
| `AT_ASKS_PER_HOUR`, `AT_HOPS_PER_MINUTE` | `10`, `30` | Per-number rate limits. |
| `AT_RETENTION_DAYS` | `7` | Days message text is kept. Opt-outs are never purged. |

</details>

---

## Data, costs and disclosures

> [!NOTE]
> **Data:** the plugin stores phone numbers and the text of questions and answers for `AT_RETENTION_DAYS` days, so answers can be read back. Tell your users. SMS and USSD traffic also passes through Africa's Talking and the mobile operators under their terms.
>
> **Costs:** Africa's Talking charges per SMS segment, and for a USSD code and shortcode in production. The sandbox is free. `AT_DAILY_SMS_CAP` bounds your spend, and `hermes ussd status` shows today's usage.

<details>
<summary><b>Catalog disclosures (rule 13)</b></summary>

- **Network:** outbound HTTPS to `api.africastalking.com` / `api.sandbox.africastalking.com` only (SMS API). No telemetry.
- **Listener:** one HTTP server on `127.0.0.1:8790` (configurable) that you expose through your own tunnel or proxy.
- **Storage:** a SQLite file in Hermes plugin storage (numbers, reply text, outbox state), mode `0600`.
- **Credentials:** read through Hermes (`get_scoped_secret`). `hermes ussd setup` writes them with Hermes's own config helper.
- **Background work:** an outbox worker and an hourly purge task inside the gateway process. No other processes, no shell commands, no self-update, no binaries.

</details>

---

## Design notes

- **Store first, send second, at most once.** A crash or ambiguous network failure marks a message `unknown`, and it is never resent (a duplicate SMS costs the recipient money). Connection failures retry at roughly 2, 8 and 30 seconds. Rejected credentials or an empty balance pause sending and keep messages queued.
- **Pure core.** The menu state machine, GSM-7 splitting, store, outbox and service have no Hermes imports and are tested directly. Only `adapter.py`, `sender.py` and `cli.py` touch Hermes.
- **Concurrent asks from one number** follow Hermes's busy-session policy. No answer is lost, but the `Ref` on an SMS may name the earlier question.

---

## Verification status

<details>
<summary><b>What is verified, and what is not (click to expand)</b></summary>

| Item | Status |
|---|---|
| Unit, property-based (Hypothesis), integration (real Hermes `PlatformEntry`, `BasePlatformAdapter`, plugin storage), chaos and security suites | Passing locally on Python 3.14 against Hermes `main` @ `cacad951`. CI runs the same, plus a pure-core job on 3.11 to 3.13. |
| Chaos: 1,000 turns with 20% provider failures | Every reply delivered once, none lost, none duplicated. Also: 10% duplicate callbacks, ambiguous network drops (never resent), total outage then recovery, balance exhaustion (paused, nothing dropped), hostile traffic. |
| `hermes plugins validate` and `hermes plugins doctor --ci` | Pass locally (`--install-deps` runs in CI). |
| Mutation check (`tools/mutation_check.py`) | 8 of 8 removed controls caught by the tests. |
| Africa's Talking **sandbox** end to end (Nov 11 2026, Hermes v0.23.1 stable, Gemini model, tunnel to a laptop) | **Verified live:** USSD menu, ask, `END` acknowledgement, agent answer by SMS (1 to 3 segments, KES 0.80 per segment, message id and cost returned by the SMS API), read-back and paging on option 2, Status, language switch, allowlist refusal. **Sandbox quirk:** the simulator posts delivery reports as `Failed` / `DeliveryFailure` for messages it did display, so delivery status is not meaningful in the sandbox. Production delivery-report behaviour is **not verified**. |
| `requires_hermes` floor | `>=0.22.5` is a conservative floor. Tested on `main` @ `cacad951` (CI) and, by hand end to end, on stable v0.23.1. |
| Kiswahili strings | First pass. Needs review by a native speaker before production use. |
| Not supported | Shared-ingress or multiplexed profiles (one listener per account and port), SMS approval codes, per-message delivery status on the USSD Status screen (the screen shows counts), voice, media. |

</details>

---

## Roadmap

- More operators and aggregators behind the same adapter.
- An optional deterministic ledger toolset for shop-keeping (record sales and debts, scheduled summaries), so small businesses can keep books from a feature phone without general tools.
- Signed-callback support if providers add it.
- More languages.

---

## Development

```bash
python -I -m pytest -q          # unit + integration + chaos + security
ruff check .
python tools/mutation_check.py  # remove each key control, expect the suite to fail
```

Licensed under MIT. See [LICENSE](LICENSE).

<div align="center">

**Hermes grows with you. Now it reaches her too.**

[Watch the demo](https://x.com/jmsonrich/status/2120585302523744356) · [Try it](#try-it-in-two-minutes) · [Security](SECURITY.md) · [Step-by-step try-it](TRY_IT.md)

</div>
