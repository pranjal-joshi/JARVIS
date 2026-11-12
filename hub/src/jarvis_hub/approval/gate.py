"""Approval gate. Read tools run freely; what the policy holds waits for a human decision from the web
UI or the phone. A voice satellite can never approve anything, so voice alone can never trigger a write
or destructive action.

Pending approvals are also written to SQLite. If the hub restarts while one is waiting, the request
that asked for it is gone, but the card isn't: it comes back as "left over", and approving it runs
the action then (through on_orphan, set by the hub), with the result announced."""

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event, EventBus

APPROVER_SOURCES = (Source.WEB, Source.PHONE)


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    # sends data off this machine (e.g. a cloud model): needs approval like a write
    EXTERNAL = "external"

    @property
    def rank(self) -> int:
        return {"read": 0, "write": 1, "external": 1, "destructive": 2}[self.value]


@dataclass
class Pending:
    id: str
    request_id: str
    source: Source
    tool: str
    risk: Risk
    arguments: dict
    reason: str = ""  # why it was held when the policy alone wouldn't have (e.g. outside text)
    says: str = ""  # the action read back in words: "send a text to Chiku saying ..."
    timeout: float = 90
    created: float = field(default_factory=time.time)
    orphan: bool = False  # left over from before a restart
    future: asyncio.Future | None = None
    preview: dict | None = None  # the effect, for the card: a diff of a note, the devices touched (preview.py)

    def public(self) -> dict:
        return {"approval_id": self.id, "request_id": self.request_id, "requested_by": self.source,
                "tool": self.tool, "risk": self.risk, "arguments": self.arguments, "created": self.created,
                "reason": self.reason, "says": self.says, "expires": None if self.orphan else self.created + self.timeout,
                "orphan": self.orphan, "preview": self.preview}


@dataclass
class Decision:
    approved: bool
    by: str  # "web" | "phone" | "timeout"


class ApprovalGate:
    def __init__(self, bus: EventBus, timeout_s: float = 90, db_path: str = ":memory:") -> None:
        self._bus = bus
        self._timeout = timeout_s
        self._pending: dict[str, Pending] = {}
        self.on_orphan: Callable[[Pending], Awaitable[None]] | None = None
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS pending_approvals (id TEXT PRIMARY KEY, body TEXT)")
            rows = self._db.execute("SELECT body FROM pending_approvals").fetchall()
        for (body,) in rows:  # waiting when the hub stopped: keep the card
            d = json.loads(body)
            p = Pending(d["approval_id"], d["request_id"], Source(d["requested_by"]), d["tool"], Risk(d["risk"]),
                        d["arguments"], d.get("reason", ""), d.get("says", ""), created=d["created"], orphan=True,
                        preview=d.get("preview"))
            self._pending[p.id] = p

    def pending(self) -> list[dict]:
        return [p.public() for p in self._pending.values()]

    async def request(self, *, request_id: str, source: Source, tool: str, risk: Risk,
                      arguments: dict, reason: str = "", says: str = "", timeout: float | None = None,
                      preview: dict | None = None) -> Decision:
        """timeout: how long this one waits (an approval asked from a satellite waits longer, in the
        background); None = the gate's own."""
        p = Pending(uuid.uuid4().hex, request_id, source, tool, risk, arguments, reason, says, timeout or self._timeout,
                    future=asyncio.get_running_loop().create_future(), preview=preview)
        self._pending[p.id] = p
        self._store(p)
        # Approval cards go to every approver channel, not only the requester.
        for approver in APPROVER_SOURCES:
            self._bus.publish(Event("approval_request", approver, request_id, p.public()))
        try:
            return await asyncio.wait_for(asyncio.shield(p.future), p.timeout)
        except TimeoutError:
            return Decision(False, "timeout")
        finally:
            self._drop(p)

    def decide(self, approval_id: str, approved: bool, by: Source) -> bool:
        if by not in APPROVER_SOURCES:
            raise PermissionError(f"{by} cannot approve actions")
        p = self._pending.get(approval_id)
        if p is None:
            return False
        if p.orphan:  # nobody is waiting for it any more: run it (or drop it) here
            self._drop(p)
            if approved and self.on_orphan:
                asyncio.get_running_loop().create_task(self.on_orphan(p))
            return True
        if p.future is None or p.future.done():
            return False
        p.future.set_result(Decision(approved, by.value))
        return True

    def get(self, approval_id: str) -> Pending | None:
        return self._pending.get(approval_id)

    def _store(self, p: Pending) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO pending_approvals VALUES (?,?)", (p.id, json.dumps(p.public())))

    def _drop(self, p: Pending) -> None:
        self._pending.pop(p.id, None)
        with self._lock, self._db:
            self._db.execute("DELETE FROM pending_approvals WHERE id=?", (p.id,))
        # decided, timed out or cancelled: approver screens drop the card
        for approver in APPROVER_SOURCES:
            self._bus.publish(Event("approval_resolved", approver, p.request_id, {"approval_id": p.id}))
