# Dixon's runbook: sandbox E2E, demo recording, submission

Everything Claude could verify without an Africa's Talking account is verified (see README "Verification status"). What is left needs your sandbox account, your machine and your voice. Do these in order; stop and tell Claude at the first thing that does not match the "expect" line.

**Deadline:** Nov 11 2026, 5 PM EST = Thu Nov 12, 01:00 Nairobi. Aim to submit by Wed 9 PM Nairobi.

## 0. Prerequisites (10 min)

* Hermes installed and a real model configured (`hermes model`), Python 3.14 runtime as Hermes uses.
* A tunnel that gives you a stable public **https** URL to `127.0.0.1:8790` (a named Cloudflare tunnel, or ngrok with a reserved domain).
* An Africa's Talking account, sandbox app: copy the **sandbox API key**; username is `sandbox`.
* Send one normal message to your Hermes (CLI is fine) **before** recording, so Hermes's one-time first-contact intro does not land in your demo.

## 1. Install and configure (5 min)

```bash
git clone https://github.com/<OWNER>/hermes-ussd && cd hermes-ussd
hermes plugins install <OWNER>/hermes-ussd        # or copy the folder into $HERMES_HOME/plugins/hermes-ussd
hermes plugins enable hermes-ussd
hermes ussd setup --username sandbox --api-key <KEY> --public-url https://<your-tunnel-host> \
    --allow <your number, e.g. +254700000001> --ussd-code '*384*1234#'
hermes config set cron.wrap_response false
hermes gateway restart        # or: hermes gateway run
hermes ussd doctor --public-url https://<your-tunnel-host>
```

**Expect:** `0 failure(s)`; credentials `ok`; callback self-test `ok`; public URL `ok`. Warnings about SANDBOX mode are normal.

In the Africa's Talking sandbox dashboard, create a USSD code and an SMS shortcode and paste the three URLs `setup` printed (`.../ussd`, `.../sms`, `.../dlr`). Treat them like passwords.

## 2. Sandbox E2E checklist (run twice, clean)

Use the sandbox **phone simulator** with your allowlisted number.

| # | Do | Expect |
|---|----|--------|
| 1 | Dial the code | `CON Hermes` menu within ~1 s. Gateway log shows `listening`, no secret in any line. |
| 2 | `1`, then type "Maize leaves yellow with streaks, Kisii" | `END Got it. ... Ref NNNN.` and the session closes |
| 3 | Wait | An SMS arrives starting `Ref NNNN:` with the answer. `hermes ussd status` shows `sent today: 1/200`. |
| 4 | Dial, `2` | The same answer on screen; `98` pages; `0` back |
| 5 | Dial, `3` | `Waiting: 0`, `SMS today: 1/200` |
| 6 | Dial from a different, non-allowlisted simulator number | `END This service is private.` and **no** SMS and no agent turn |
| 7 | Text a question to the shortcode | An SMS reply (no `Ref`) |
| 8 | Text `STOP`, then ask on USSD | `END You opted out of SMS...`; text `START` to rejoin |
| 9 | Delivery report | Check `hermes ussd status`: outbox shows `sent`; look at the gateway log for the DLR callback |
| 10 | Wrong secret: `curl -i -X POST https://<host>/wrong/ussd -d x=1` | `404`, empty body |

**Record what Africa's Talking really sends** (this is the biggest unverified item): for steps 2, 3 and 9 copy the exact form fields of the callbacks and the JSON the SMS API returns (statusCode, messageId, cost) into a note and send it to Claude. The status-code table, DLR field names and the 401/405 behaviour were written from documentation; the sandbox is the first real test. Specifically confirm: (a) `text` accumulates as `1*question`, (b) the SMS API response has `SMSMessageData.Recipients[0].statusCode` 101, (c) DLR posts `id` and `status`.

If step 3 never arrives: `hermes ussd status` and `hermes ussd doctor`. `unknown` means a network error after send (check the proxy/egress), `queued` with the health line `auth` means the API key was rejected.

## 3. The demo (target 85 s, hard limit 90 s)

The scenario is a farmer with a basic phone. Layout: sandbox phone simulator on the left, terminal on the right, burned-in captions (judges often watch muted). Record at 1080p/30 fps; record the voice-over separately.

| Time | Shot | Voice-over | Caption |
|------|------|------------|---------|
| 0:00-0:07 | A basic phone (or the simulator) | "This phone can't run an app. Now it runs Hermes." | Hermes for every phone |
| 0:07-0:20 | Dial the code; the menu appears; `1` Ask; type "Maize leaves yellow with streaks, Kisii" | "Dial a code, pick Ask, type like a text." | Menu answered instantly, no model in the loop |
| 0:20-0:24 | `END Got it ... Ref NNNN` | "The session closes at once. Hermes keeps working." | USSD can't wait for an agent. So it doesn't. |
| 0:24-0:36 | The SMS with the answer arrives in the simulator | "The answer comes back as a text." | Same agent, any phone |
| 0:36-0:52 | Dial, `2`, page 1, `98`, page 2 | "Long answers? Read them on the menu, page by page." | Stored first, sent second |
| 0:52-1:04 | Terminal: `hermes cron create "0 7 * * *" "One-line maize tip for Kisii farmers" --deliver africastalking:+254...`; then `hermes cron run <id>`; SMS arrives | "And Hermes can text first." | Scheduled by the operator; delivered by SMS |
| 1:04-1:16 | Terminal: `hermes config set AT_API_KEY wrong`, restart gateway; ask again; the SMS does not come; dial `3` shows `Waiting: 1`; dial `2` shows the answer anyway | "Networks fail. Answers don't get lost." | Zero lost replies in the 1,000-turn chaos test |
| 1:16-1:25 | Terminal: `hermes plugins install ...`, then `hermes ussd simulate --number +254...` showing the text phone | "Try it in a minute, no account needed for the menu." | Open source, MIT |
| 1:25-1:28 | End card | "hermes-ussd. Built in Kisumu." | Dixon Osure, github link |

Notes:

* Make the cron shot real. Do not fake an SMS. Cut waiting time but add a small caption: "Sandbox simulator, waits shortened".
* For the failure shot, **restore the key and restart the gateway afterwards**; the held message is then delivered (the store survives restarts), which you can show or mention.
* The chaos claim on the caption is true of the repo's test suite (`tests/test_chaos.py`); you can show it passing in a second terminal tab for two seconds.
* Keep two clean takes of every shot before editing. Final length check: under 90 s including the end card.
* The sandbox simulator is the honest stage: the PR says a live USSD code needs operator approval.

## 4. Submission (Wednesday)

1. Create the **public** repo `<OWNER>/hermes-ussd`, push this folder (MIT licence is included), turn on **private vulnerability reporting** (Settings, Code security).
2. Wait for CI to go green. Fix anything red (send Claude the log). The `hermes` job runs `validate --install-deps` and `doctor --ci`.
3. Take the 40-character SHA of the green commit. Put it in `submission/hermes-ussd.yaml` with your GitHub handle for `<OWNER>`. **Do not change code after pinning without re-pinning**; the SHA is the trust model.
4. Fork `NousResearch/hermes-agent`, add `plugin-catalog/hermes-ussd.yaml` (re-read `plugin-catalog/README.md` first in case the schema moved), open the PR using `submission/PR_DESCRIPTION.md`; fill in the `<LINK>`, `<N>`, `<DATE>` placeholders.
5. Upload the demo (<90 s) where the form accepts it, submit the Typeform with the PR link.
6. Post the demo on X tagging @NousResearch; join the Discord thread Nous opens for each entry.

## 5. If something breaks

| Symptom | First move |
|---------|------------|
| Dial gives no menu | `hermes ussd doctor --public-url ...`; is the tunnel pointing at 8790? URL has the current secret? |
| `END This service is private.` for you | Your number is not in `AT_ALLOWED_USERS`, or it is written differently (+254 vs 07). `hermes ussd status` shows the allowlist size. |
| Answer on USSD but no SMS | `hermes ussd status`: `unknown`/`failed_*` tells the story. Health `auth` = key rejected; `balance` = sandbox balance. |
| Anything strange | Send Claude the doctor output and `hermes ussd status`. Never paste the full webhook URLs or the API key. |
