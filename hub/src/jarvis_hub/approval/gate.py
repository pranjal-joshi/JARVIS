"""Approval gate. Read tools run freely; write and destructive tools wait for
a human decision from the web UI or the phone. A voice satellite can never
approve anything, so voice alone can never trigger a write or destructive action."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event, EventBus

APPROVER_SOURCES = (Source.WEB, Source.PHONE)


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @property
    def rank(self) -> int:
        return {"read": 0, "write": 1, "destructive": 2}[self.value]


@dataclass
class Pending:
    id: str
    request_id: str
    source: Source
    tool: str
    risk: Risk
    arguments: dict
    created: float = field(default_factory=time.time)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def public(self) -> dict:
        return {"approval_id": self.id, "request_id": self.request_id,
                "requested_by": self.source, "tool": self.tool, "risk": self.risk,
                "arguments": self.arguments, "created": self.created}


@dataclass
class Decision:
    approved: bool
    by: str  # "web" | "phone" | "timeout"


class ApprovalGate:
    def __init__(self, bus: EventBus, timeout_s: float = 90) -> None:
        self._bus = bus
        self._timeout = timeout_s
        self._pending: dict[str, Pending] = {}

    def pending(self) -> list[dict]:
        return [p.public() for p in self._pending.values()]

    async def request(self, *, request_id: str, source: Source, tool: str, risk: Risk,
                      arguments: dict) -> Decision:
        p = Pending(uuid.uuid4().hex, request_id, source, tool, risk, arguments)
        self._pending[p.id] = p
        # Approval cards go to every approver channel, not only the requester.
        for approver in APPROVER_SOURCES:
            self._bus.publish(Event("approval_request", approver, request_id, p.public()))
        try:
            return await asyncio.wait_for(asyncio.shield(p.future), self._timeout)
        except TimeoutError:
            return Decision(False, "timeout")
        finally:
            self._pending.pop(p.id, None)

    def decide(self, approval_id: str, approved: bool, by: Source) -> bool:
        if by not in APPROVER_SOURCES:
            raise PermissionError(f"{by} cannot approve actions")
        p = self._pending.get(approval_id)
        if p is None or p.future.done():
            return False
        p.future.set_result(Decision(approved, by.value))
        return True
