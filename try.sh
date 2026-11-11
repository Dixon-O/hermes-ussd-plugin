#!/usr/bin/env bash
# hermes-ussd: try it with ONE command. No Africa's Talking account, no phone, no tunnel.
#   bash try.sh
# It installs the plugin, starts Hermes, and gives you a pretend phone in this terminal.
# Needs: Hermes installed with a model chosen (hermes model). Linux, macOS or WSL.
set -u

PORT="${TRY_PORT:-8790}"
NUMBER="+254700000001"
REPO="${TRY_REPO:-Dixon-O/hermes-ussd}"
LOG="$(mktemp -t hermes-ussd-try.XXXXXX)"
GATEWAY_PID=""

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
cleanup() { [ -n "$GATEWAY_PID" ] && kill "$GATEWAY_PID" 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT

if ! command -v hermes >/dev/null 2>&1; then
  say "Step 0: install Hermes first (one time)"
  echo "  curl -fsSLO https://hermes-agent.nousresearch.com/install.sh"
  echo "  bash install.sh"
  echo "Open a new terminal, run:  hermes model   (pick any model), then run this script again."
  exit 1
fi

say "1/4  Installing the plugin from GitHub ($REPO)"
if ! hermes plugins list 2>/dev/null | grep -q "hermes-ussd"; then
  hermes plugins install "$REPO" --enable --yes-deps < /dev/null || { echo "Install failed. See the message above."; exit 1; }
else
  hermes plugins enable hermes-ussd < /dev/null >/dev/null 2>&1 || true
  echo "Already installed."
fi

say "2/4  Saving test settings (a pretend Africa's Talking account; nothing real is contacted)"
SECRET="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
hermes config set AT_USERNAME sandbox >/dev/null
hermes config set AT_API_KEY dummy-key-for-local-testing >/dev/null
hermes config set AT_WEBHOOK_SECRET "$SECRET" >/dev/null
hermes config set AT_ALLOWED_USERS "$NUMBER" >/dev/null
hermes config set AT_WEBHOOK_PORT "$PORT" >/dev/null
hermes config set AT_ENV sandbox >/dev/null
echo "Pretend phone number: $NUMBER"

say "3/4  Starting Hermes (this takes up to a minute)"
hermes gateway run > "$LOG" 2>&1 < /dev/null &
GATEWAY_PID=$!
ready=""
for _ in $(seq 1 60); do
  if hermes ussd simulate --number "$NUMBER" --path "" 2>/dev/null | grep -q "Hermes"; then ready=yes; break; fi
  kill -0 "$GATEWAY_PID" 2>/dev/null || break
  sleep 2
done
if [ -z "$ready" ]; then
  echo "Hermes did not start. Last log lines (log file: $LOG):"; tail -n 15 "$LOG"; exit 1
fi

say "4/4  Your pretend phone is ready"
cat <<'GUIDE'
  What to do:
    1) Type 1 and press Enter           (Ask a question)
    2) Type any question, press Enter   (e.g. Maize leaves yellow with streaks, Kisii)
    3) The session ends: the answer would arrive by SMS.
    4) Run again and choose 2           (Read last answer) to see the answer on the menu.
  Press Ctrl-C to hang up. No model yet? Run "hermes model" in another terminal, then retry.

GUIDE
if [ ! -t 0 ]; then
  echo "(no keyboard attached: running a scripted call instead)"
  hermes ussd simulate --number "$NUMBER" --path ""
  hermes ussd simulate --number "$NUMBER" --path "1*Maize leaves yellow with streaks, Kisii"
  sleep 25
  hermes ussd simulate --number "$NUMBER" --path "2"
  exit 0
fi
hermes ussd simulate --number "$NUMBER"
# Second dial: read the stored answer.
printf '\nDial again to read the answer? (Y/n) '; read -r again
case "${again:-y}" in n|N) ;; *) hermes ussd simulate --number "$NUMBER" ;; esac
say "Done. Hermes is stopped. Re-run 'bash try.sh' any time."
