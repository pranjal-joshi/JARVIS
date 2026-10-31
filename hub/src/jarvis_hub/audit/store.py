"""Audit log of every tool call. SQLite for the POC; the schema avoids
SQLite-only types so it ports to Postgres unchanged."""

import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY,
    ts            DOUBLE PRECISION NOT NULL,
    request_id    TEXT NOT NULL,
    source        TEXT NOT NULL,
    tool          TEXT NOT NULL,
    risk          TEXT NOT NULL,      -- read | write | destructive
    arguments     TEXT NOT NULL,      -- JSON
    approved_by   TEXT,               -- null for read tools
    status        TEXT NOT NULL,      -- requested | approved | denied | ok | error
    result        TEXT
);
CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts);
"""


class AuditStore:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def record(self, *, request_id: str, source: str, tool: str, risk: str,
               arguments: dict, status: str, approved_by: str | None = None,
               result: str | None = None) -> str:
        row_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row_id, time.time(), request_id, source, tool, risk,
                 json.dumps(arguments), approved_by, status, result),
            )
        return row_id

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def between(self, start: float, end: float) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM audit_log WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
