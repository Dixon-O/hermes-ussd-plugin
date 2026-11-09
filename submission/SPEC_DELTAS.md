# Build vs spec: what changed and why

Spec: "hermes-ussd: Implementation Plan & Build Spec". Status as of this commit.

## Delivered
All P0 features F1-F9; P1 F10 (delivery reports recorded; see below), F12 (English/Kiswahili per number), F13 (SMS-writing skill). Security controls T1-T7, T9-T15 (T8 not applicable, below). Robustness: store-first outbox, at-most-once, crash recovery, circuit breaker, caps, opt-outs, retention.

## Deviations (each is deliberate)
| Spec | Built | Why |
|------|-------|-----|
| F11 / T8 approvals over SMS with 6-digit codes (P1) | Not built | Hermes has no public hook for a platform to resolve approvals from a custom code; phone sessions are pinned to the `safe` toolset so nothing needs approval; Hermes approvals are never bypassed. Roadmap. |
| Hero demo: shop owner keeps a ledger, asks to be texted totals at 9 PM | Farmer scenario; operator-created cron job for the proactive SMS | The `safe` toolset has no memory or cron tools, and enabling `cronjob` for phone users is an escalation (found by running a real cron job: Hermes gives cron jobs the full toolset), so `doctor` fails on it. The ledger toolset (F15) is roadmap. |
| F10 USSD Status shows sent/delivered/failed per message | Status shows waiting count and SMS used today | DLRs are stored in the outbox; per-message screen is a small follow-up. |
| `hermes ussd simulate --fail-sms 100%` | Not built | Outage for the demo is produced with a wrong `AT_API_KEY` (the real auth circuit breaker); chaos behaviour is shown by `tests/test_chaos.py`. |
| T5 per-IP buckets, bounded in-flight task set with `END Busy` | Per-number buckets, body/time limits, bounded retries; no global in-flight cap | Per-IP is meaningless behind a tunnel (every request arrives from the tunnel); Hermes manages agent concurrency. Documented in SECURITY.md. |
| T3 text NFC normalisation | Length caps, control-character handling in the menu and GSM-7 normalisation on output | Hypothesis fuzzing and hostile-input tests pass. |
| `AT_ENV=live` | `AT_ENV=production` | Matches Africa's Talking's own wording. |
| CI on Python 3.11/3.12 with mypy --strict | Pure-core job on 3.11-3.13, full Hermes job on 3.14, ruff; no mypy | Hermes' runtime is Python 3.14 and the integration tests need it; mypy is a possible follow-up. |
| `plugin.yaml` `capabilities:` block | Omitted | The manifest key is a list of consent capability ids (none apply); the dict form belongs to the catalog entry only. Found by running the real gateway. |

## Bugs found only by running against the real Hermes gateway (all fixed, all have regression tests)
1. Events carried adapter `metadata`; the gateway's native-admission check refuses that for non-WhatsApp platforms, so no turn ever ran.
2. Hermes's one-time "no home channel is set, type /sethome" notice was texted to users and consumed the question's `Ref`. Notices are now routed privately and dropped.
3. `plugin.yaml` `capabilities` was a dict (ignored with a warning).
4. Test-order dependency: `Platform("africastalking")` only resolved if another test had registered it first.

## Still unverified (needs your sandbox)
Real Africa's Talking payloads and SMS-API status codes, delivery-report fields, USSD timeout behaviour, `requires_hermes` on 0.22.5 (tested on `main` only). See `DEMO_RUNBOOK.md` section 2.
