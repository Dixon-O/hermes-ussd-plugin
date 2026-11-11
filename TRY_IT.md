# Try hermes-ussd in 2 minutes

You do not need a phone, an Africa's Talking account or a tunnel. You get a **pretend phone inside your terminal**.

**You need:** Hermes installed with a model chosen. Windows users: use WSL (Ubuntu).
Not installed yet?

```bash
curl -fsSLO https://hermes-agent.nousresearch.com/install.sh
bash install.sh
hermes model        # pick any model, paste its key
```

**Then, one command:**

```bash
git clone https://github.com/Dixon-O/hermes-ussd-plugin
cd hermes-ussd-plugin
bash try.sh
```

It installs the plugin, starts Hermes, and shows a phone menu:

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

1. Type `1`, press Enter.
2. Type a question, for example `Maize leaves yellow with streaks, Kisii`, press Enter.
3. You see "Got it. Your answer will come by SMS. Ref 1234." The session ends, just like a real USSD session. On a real phone the answer arrives as a text message.
4. Say `Y` when it asks to dial again, then choose `2`. Your answer is on the screen (it is stored first, texted second, so it is never lost).

Everything is local. The fake SMS credentials contact nothing, and only the test number `+254700000001` is allowed (the service is closed to everyone else by default).

**What this does not show:** the real text message arriving on a phone. That needs an Africa's Talking account. See the demo video, or the "Setting up with the Africa's Talking sandbox" section of the README (free, about 15 minutes).

**Something wrong?**
- "Sorry, I could not answer that just now": your Hermes has no working model. Run `hermes model`, then try again.
- Hermes did not start: the script prints the log. Another Hermes gateway on the same machine may be using port 8790; run `TRY_PORT=8791 bash try.sh`.
- Undo everything: `hermes plugins disable hermes-ussd`.
