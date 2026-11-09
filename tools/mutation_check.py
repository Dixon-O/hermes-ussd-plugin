"""Mutation sanity check: removing any key security/reliability control must make the suite fail.

Run from the repo root:  python tools/mutation_check.py   (needs the dev dependencies; ~1 min)
"""
import pathlib, shutil, subprocess, sys, tempfile

SRC = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
MUTATIONS = [
    ("allowlist open to everyone", "service.py",
     "        return s.allow_all or number in s.allowed", "        return True"),
    ("webhook secret not checked", "webhooks.py",
     'return hmac.compare_digest(candidate.encode("utf-8", "replace"), secret.encode("utf-8", "replace"))',
     "return True"),
    ("ambiguous failure retried (would double-send)", "sms/client.py",
     'return Outcome("unknown", detail=f"transport error after send: {type(exc).__name__}")',
     'return Outcome("retry", detail="x")'),
    ("replayed USSD hop re-submits the turn", "service.py",
     "        if is_new:\n            self.store.add_ask(ref, number, \"ussd\")",
     "        if True:\n            self.store.add_ask(ref, number, \"ussd\")"),
    ("daily cap ignored", "outbox.py",
     "        if cap <= 0:\n            return True\n        return self.store.counter(st.day_key(self.clock(), self.settings.day_offset_hours)) >= cap",
     "        return False"),
    ("opt-out ignored", "outbox.py",
     "        if self.store.is_opted_out(row.chat_id):", "        if False:"),
    ("outbound target not restricted", "sender.py",
     "    if allow_all or n in allowed or n == home:", "    if True:"),
    ("USSD text allowed to act as gateway command", "service.py",
     'self._submit(number, question, "ussd", f"ussd:{ref}", False)',
     'self._submit(number, question, "ussd", f"ussd:{ref}", True)'),
]
survived = []
for name, rel, old, new in MUTATIONS:
    with tempfile.TemporaryDirectory() as d:
        dst = pathlib.Path(d) / "hermes-ussd"
        shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(".git", ".hypothesis", "__pycache__", ".ruff_cache"))
        f = dst / rel
        text = f.read_text()
        if text.count(old) != 1:
            print(f"?? {name}: pattern found {text.count(old)}x in {rel}"); survived.append(name); continue
        f.write_text(text.replace(old, new))
        r = subprocess.run([PY, "-I", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--ignore=tests/test_chaos.py"],
                           cwd=dst, capture_output=True, text=True, timeout=280)
        killed = r.returncode != 0
        print(("KILLED  " if killed else "SURVIVED"), name)
        if not killed:
            survived.append(name)
print("\nsurvivors:", survived or "none")
sys.exit(1 if survived else 0)
