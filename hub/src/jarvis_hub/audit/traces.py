"""Traces: every turn of every conversation as the model saw it, kept so the harness can be improved from
real use. A turn holds what the client sent, the tools offered, each model call's output, the full
conversation the model ended with (tool calls and results included), the policy decisions and the reply.

Turns are grouped by session: the HUD sends an id per "New session", a satellite's conversation gets one
from when it started, anything else is a session of one turn. Kept in their own SQLite file next to the
hub's database for TRACE_DAYS days (0 = not kept). Personal message text stays out, as in the audit."""

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    request_id  TEXT PRIMARY KEY,
    ts          DOUBLE PRECISION NOT NULL,
    session     TEXT NOT NULL,
    source      TEXT NOT NULL,
    route       TEXT NOT NULL,      -- fast | memory | confirm | model | plan
    model       TEXT,
    seconds     DOUBLE PRECISION,
    said        TEXT NOT NULL,      -- the user's latest words
    reply       TEXT NOT NULL,
    data        TEXT NOT NULL       -- JSON: messages, loops, trace, parts...
);
CREATE INDEX IF NOT EXISTS turns_session ON turns (session, ts);
CREATE INDEX IF NOT EXISTS turns_ts ON turns (ts);
"""


class TraceStore:
    def __init__(self, path: str, days: float = 30) -> None:
        self.days = days
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
        self.prune()

    def record(self, *, request_id: str, session: str, source: str, route: str, model: str, seconds: float,
               said: str, reply: str, data: dict) -> None:
        if self.days <= 0:
            return
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (request_id, time.time(), session, source, route, model, seconds, said, reply,
                                json.dumps(data, ensure_ascii=False, default=str)))

    def sessions(self, limit: int = 50) -> list[dict]:
        """Newest first: id, when it started and last changed, how many turns, and its first words."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT session, MIN(ts), MAX(ts), COUNT(*), "
                "(SELECT said FROM turns t2 WHERE t2.session = t1.session ORDER BY ts LIMIT 1) "
                "FROM turns t1 GROUP BY session ORDER BY MAX(ts) DESC LIMIT ?", (limit,))
            return [{"session": r[0], "started": r[1], "last": r[2], "turns": r[3], "first": r[4]}
                    for r in cur.fetchall()]

    def session(self, session: str) -> list[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM turns WHERE session = ? ORDER BY ts", (session,))
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def prune(self) -> int:
        if self.days <= 0:
            return 0
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM turns WHERE ts < ?",
                                      (time.time() - self.days * 86400,)).rowcount

    def close(self) -> None:
        self._conn.close()
