"""In-process event bus. Every event carries the source it belongs to, and
subscribers only receive events for their own source."""

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from jarvis_hub.config import Source

EventType = Literal[
    "thinking", "kb_retrieved", "tool_call", "tool_result", "reply", "approval_request", "approval_resolved", "timer",
    "hook_event", "skill_run", "skill_done", "announce", "trace", "notify", "watch", "timer_state"
]


@dataclass
class Event:
    type: EventType
    source: Source
    request_id: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return asdict(self)


class EventBus:
    def __init__(self, queue_size: int = 256) -> None:
        self._subs: dict[Source, set[asyncio.Queue[Event]]] = {s: set() for s in Source}
        self._queue_size = queue_size
        self._taps: list = []

    def subscribe(self, source: Source) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(self._queue_size)
        self._subs[source].add(q)
        return q

    def unsubscribe(self, source: Source, q: asyncio.Queue[Event]) -> None:
        self._subs[source].discard(q)

    def tap(self, fn) -> None:
        """Call fn(event) for every event, whatever its source (in-process automation only)."""
        self._taps.append(fn)

    def publish(self, event: Event) -> None:
        for fn in self._taps:
            fn(event)
        for q in self._subs[event.source]:
            if q.full():  # drop the oldest rather than block the agent
                q.get_nowait()
            q.put_nowait(event)
