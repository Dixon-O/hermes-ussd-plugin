"""SQLite persistence: replies, asks, outbox, dedupe, opt-outs, prefs, counters. No Hermes imports.

The store takes a ready ``sqlite3.Connection`` so tests can use a temp file and the plugin can hand it
the connection from ``plugins.plugin_storage.plugin_db``. All access happens on the gateway's single
event-loop thread (or in a short-lived CLI process); WAL lets the two coexist.

Design rules that matter for correctness:
* Replies are stored *before* any SMS is attempted, so USSD "read last answer" works even when every
  SMS fails.
* ``outbox.idem_key`` is UNIQUE, so re-enqueueing the same reply segment is a no-op.
* A row is marked ``sending`` before the HTTP call; a crash leaves it ``sending`` and recovery turns it
  into ``unknown`` rather than ever resending (at-most-once).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

SCHEMA_VERSION = 1

TERMINAL_OK = ("sent", "delivered")
LIVE = ("queued", "sending")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS asks(
  ref TEXT PRIMARY KEY, chat_id TEXT NOT NULL, origin TEXT NOT NULL,
  created_at REAL NOT NULL, answered_at REAL);
CREATE INDEX IF NOT EXISTS asks_chat ON asks(chat_id, answered_at, created_at);
CREATE TABLE IF NOT EXISTS replies(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT NOT NULL, chat_id TEXT NOT NULL,
  text TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0, segments INTEGER NOT NULL,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS replies_chat ON replies(chat_id, id DESC);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT NOT NULL UNIQUE, ref TEXT NOT NULL,
  chat_id TEXT NOT NULL, segment_no INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
  at_message_id TEXT, cost TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS outbox_mid ON outbox(at_message_id);
CREATE INDEX IF NOT EXISTS outbox_ref ON outbox(chat_id, ref, segment_no);
CREATE TABLE IF NOT EXISTS inbound_seen(key TEXT PRIMARY KEY, ref TEXT, seen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS optouts(chat_id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS prefs(chat_id TEXT PRIMARY KEY, lang TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(k TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""

# Fixed statements (no string-built SQL anywhere in this module).
_ERASE_BY_NUMBER = (
    ("replies", "DELETE FROM replies WHERE chat_id=?"),
    ("asks", "DELETE FROM asks WHERE chat_id=?"),
    ("outbox", "DELETE FROM outbox WHERE chat_id=?"),
    ("prefs", "DELETE FROM prefs WHERE chat_id=?"),
)

ASK_TTL = 30 * 60  # an unanswered ask older than this no longer claims the next reply
INBOUND_TTL = 48 * 3600


def idem_key(chat_id: str, ref: str, segment_no: int) -> str:
    return hashlib.sha256(f"{chat_id}|{ref}|{segment_no}".encode()).hexdigest()


@dataclass(frozen=True)
class OutboxRow:
    id: int
    ref: str
    chat_id: str
    segment_no: int
    body: str
    status: str
    attempts: int
    at_message_id: Optional[str]


class Store:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.clock = clock
        conn.row_factory = sqlite3.Row

    # -- lifecycle ---------------------------------------------------------
    def migrate(self) -> None:
        with self.conn:
            self.conn.executescript(_SCHEMA)
            row = self.conn.execute("SELECT v FROM schema_meta WHERE k='version'").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO schema_meta(k, v) VALUES('version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["v"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"hermes-ussd database is schema v{row['v']}, newer than this plugin (v{SCHEMA_VERSION})")

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # -- asks / refs -------------------------------------------------------
    def new_ref(self) -> str:
        """A fresh 4-digit reference not held by any live ask."""
        for _ in range(50):
            ref = str(1000 + secrets.randbelow(9000))
            if self.conn.execute("SELECT 1 FROM asks WHERE ref=?", (ref,)).fetchone() is None:
                return ref
        return str(10000 + secrets.randbelow(90000))  # crowded: widen rather than collide

    def add_ask(self, ref: str, chat_id: str, origin: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO asks(ref, chat_id, origin, created_at) VALUES(?,?,?,?)",
                (ref, chat_id, origin, self.clock()))

    def claim_oldest_ask(self, chat_id: str) -> Optional[tuple[str, str]]:
        """Mark the oldest live unanswered ask for ``chat_id`` answered; returns ``(ref, origin)``."""
        now = self.clock()
        with self.conn:
            row = self.conn.execute(
                "SELECT ref, origin FROM asks WHERE chat_id=? AND answered_at IS NULL AND created_at>=? "
                "ORDER BY created_at, rowid LIMIT 1", (chat_id, now - ASK_TTL)).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE asks SET answered_at=? WHERE ref=?", (now, row["ref"]))
            return row["ref"], row["origin"]

    def pending_asks(self, chat_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM asks WHERE chat_id=? AND answered_at IS NULL AND created_at>=?",
            (chat_id, self.clock() - ASK_TTL)).fetchone()
        return int(row["n"])

    # -- inbound dedupe ----------------------------------------------------
    def claim_inbound(self, key: str, ref: Optional[str] = None) -> tuple[bool, Optional[str]]:
        """Record ``key``; ``(True, ref)`` when new, ``(False, stored_ref)`` for a replay."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO inbound_seen(key, ref, seen_at) VALUES(?,?,?)", (key, ref, self.clock()))
            if cur.rowcount == 1:
                return True, ref
            row = self.conn.execute("SELECT ref FROM inbound_seen WHERE key=?", (key,)).fetchone()
            return False, (row["ref"] if row else None)

    # -- replies -----------------------------------------------------------
    def put_reply(self, ref: str, chat_id: str, text: str, truncated: bool, segments: int) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO replies(ref, chat_id, text, truncated, segments, created_at) VALUES(?,?,?,?,?,?)",
                (ref, chat_id, text, int(truncated), segments, self.clock()))
            return int(cur.lastrowid)

    def last_reply(self, chat_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT text FROM replies WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        return row["text"] if row else None

    # -- outbox ------------------------------------------------------------
    def enqueue(self, chat_id: str, ref: str, segments: Iterable[str]) -> list[int]:
        """Insert one row per segment; replays are no-ops. Returns ids of rows that now exist."""
        now = self.clock()
        ids: list[int] = []
        with self.conn:
            for n, body in enumerate(segments, start=1):
                key = idem_key(chat_id, ref, n)
                self.conn.execute(
                    "INSERT OR IGNORE INTO outbox(idem_key, ref, chat_id, segment_no, body, status, "
                    "next_attempt_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, ref, chat_id, n, body, "queued", now, now, now))
                ids.append(int(self.conn.execute(
                    "SELECT id FROM outbox WHERE idem_key=?", (key,)).fetchone()["id"]))
        return ids

    def due(self, limit: int = 20) -> list[OutboxRow]:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE status='queued' AND next_attempt_at<=? ORDER BY id LIMIT ?",
            (self.clock(), limit)).fetchall()
        return [self._row(r) for r in rows]

    def next_due_at(self) -> Optional[float]:
        row = self.conn.execute("SELECT MIN(next_attempt_at) AS t FROM outbox WHERE status='queued'").fetchone()
        return row["t"] if row and row["t"] is not None else None

    def prior_segments_ok(self, row: OutboxRow) -> Optional[bool]:
        """True when every earlier segment of this reply went out, False when one failed for good,
        None while an earlier segment is still pending (do not send this one yet)."""
        prior = self.conn.execute(
            "SELECT status FROM outbox WHERE chat_id=? AND ref=? AND segment_no<?",
            (row.chat_id, row.ref, row.segment_no)).fetchall()
        statuses = [p["status"] for p in prior]
        if any(s in LIVE for s in statuses):
            return None
        return all(s in TERMINAL_OK for s in statuses)

    def mark_sending(self, row_id: int) -> bool:
        """Claim a queued row; False when another worker got it (never double-send)."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outbox SET status='sending', attempts=attempts+1, updated_at=? "
                "WHERE id=? AND status='queued'", (self.clock(), row_id))
            return cur.rowcount == 1

    def finish(self, row_id: int, status: str, *, message_id: Optional[str] = None,
               cost: Optional[str] = None, error: Optional[str] = None,
               retry_at: Optional[float] = None, refund_attempt: bool = False) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status=?, at_message_id=COALESCE(?, at_message_id), cost=COALESCE(?, cost), "
                "last_error=?, next_attempt_at=COALESCE(?, next_attempt_at), updated_at=?, "
                "attempts=MAX(0, attempts-?) WHERE id=?",
                (status, message_id, cost, (error or None) and error[:300], retry_at, self.clock(),
                 1 if refund_attempt else 0, row_id))

    def recover_sending(self) -> int:
        """After a crash: rows left ``sending`` may or may not have gone out. Never resend them."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outbox SET status='unknown', last_error='interrupted while sending', updated_at=? "
                "WHERE status='sending'", (self.clock(),))
            return cur.rowcount

    def apply_dlr(self, message_id: str, status: str, reason: Optional[str]) -> bool:
        """Delivery report for a message we sent. ``status`` is Africa's Talking's own word."""
        s = (status or "").strip().lower()
        if s in ("success", "delivered"):
            new = "delivered"
        elif s in ("failed", "rejected", "expired", "undelivered"):
            new = "failed_delivery"
        else:
            return False  # Sent/Submitted/Buffered: nothing to change
        with self.conn:
            cur = self.conn.execute(
                "UPDATE outbox SET status=?, last_error=COALESCE(?, last_error), updated_at=? "
                "WHERE at_message_id=? AND status IN ('sent','delivered')",
                (new, (reason or None) and reason[:200], self.clock(), message_id))
            return cur.rowcount > 0

    def queued_count(self, chat_id: Optional[str] = None) -> int:
        if chat_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM outbox WHERE status IN ('queued','sending')").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE chat_id=? AND status IN ('queued','sending')",
                (chat_id,)).fetchone()
        return int(row["n"])

    def status_counts(self) -> dict[str, int]:
        return {r["status"]: int(r["n"]) for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")}

    # -- opt-outs / prefs --------------------------------------------------
    def opt_out(self, chat_id: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO optouts(chat_id, at) VALUES(?,?)", (chat_id, self.clock()))

    def opt_in(self, chat_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM optouts WHERE chat_id=?", (chat_id,))

    def is_opted_out(self, chat_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM optouts WHERE chat_id=?", (chat_id,)).fetchone() is not None

    def get_lang(self, chat_id: str) -> Optional[str]:
        row = self.conn.execute("SELECT lang FROM prefs WHERE chat_id=?", (chat_id,)).fetchone()
        return row["lang"] if row else None

    def set_lang(self, chat_id: str, lang: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO prefs(chat_id, lang) VALUES(?,?)", (chat_id, lang))

    # -- counters ----------------------------------------------------------
    def incr(self, key: str, by: int = 1) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT INTO counters(k, n) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET n=n+?", (key, by, by))
            return int(self.conn.execute("SELECT n FROM counters WHERE k=?", (key,)).fetchone()["n"])

    def counter(self, key: str) -> int:
        row = self.conn.execute("SELECT n FROM counters WHERE k=?", (key,)).fetchone()
        return int(row["n"]) if row else 0

    # -- retention / privacy -----------------------------------------------
    def purge(self, retention_days: int, batch: int = 500) -> dict[str, int]:
        """Delete aged data in bounded batches. Opt-outs are never purged (compliance)."""
        now = self.clock()
        cutoff = now - max(1, retention_days) * 86400
        removed = {"replies": 0, "asks": 0, "outbox": 0, "inbound_seen": 0}
        plan = (
            ("replies", "DELETE FROM replies WHERE id IN (SELECT id FROM replies WHERE created_at<? LIMIT ?)", cutoff),
            ("asks", "DELETE FROM asks WHERE ref IN (SELECT ref FROM asks WHERE created_at<? LIMIT ?)", cutoff),
            ("outbox", "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE created_at<? "
                       "AND status NOT IN ('queued','sending') LIMIT ?)", cutoff),
            ("inbound_seen", "DELETE FROM inbound_seen WHERE key IN (SELECT key FROM inbound_seen "
                             "WHERE seen_at<? LIMIT ?)", now - INBOUND_TTL),
        )
        for name, sql, bound in plan:
            while True:
                with self.conn:
                    n = self.conn.execute(sql, (bound, batch)).rowcount
                removed[name] += n
                if n < batch:
                    break
        with self.conn:
            self.conn.execute("DELETE FROM counters WHERE k LIKE 'sent:%' AND k < ?",
                              (f"sent:{time.strftime('%Y%m%d', time.gmtime(now - 40 * 86400))}",))
        return removed

    def purge_number(self, chat_id: str, include_optout: bool = False) -> dict[str, int]:
        """Right-to-erasure for one number. The opt-out record stays unless explicitly included, so
        a person who said STOP is not texted again after being forgotten."""
        out: dict[str, int] = {}
        with self.conn:
            for table, sql in _ERASE_BY_NUMBER:
                out[table] = self.conn.execute(sql, (chat_id,)).rowcount
            if include_optout:
                out["optouts"] = self.conn.execute("DELETE FROM optouts WHERE chat_id=?", (chat_id,)).rowcount
        return out

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> OutboxRow:
        return OutboxRow(r["id"], r["ref"], r["chat_id"], r["segment_no"], r["body"], r["status"],
                         r["attempts"], r["at_message_id"])


def day_key(now: float, offset_hours: int = 3) -> str:
    """``sent:YYYYMMDD`` for the operator's local day (default UTC+3, Kenya)."""
    return "sent:" + time.strftime("%Y%m%d", time.gmtime(now + offset_hours * 3600))
