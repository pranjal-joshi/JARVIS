"""Agent loop interface. The API layer only talks to `Agent`, so the loop can
be swapped later (e.g. for Hermes Agent) without touching anything else."""

from typing import Protocol

from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event, EventBus


class Agent(Protocol):
    async def run(self, *, messages: list[dict], source: Source, request_id: str,
                  bus: EventBus, on_text=None, on_event=None, session: str | None = None) -> str: ...


class EchoAgent:
    """Phase 1 stand-in: exercises the event path without an LLM."""

    async def run(self, *, messages: list[dict], source: Source, request_id: str,
                  bus: EventBus, on_text=None, on_event=None, session: str | None = None) -> str:
        bus.publish(Event("thinking", source, request_id))
        last = next((m.get("content") or "" for m in reversed(messages)
                     if m.get("role") == "user"), "")
        reply = f"Echo, sir: {last}" if last else "I am listening, sir."
        bus.publish(Event("reply", source, request_id, {"text": reply}))
        return reply
