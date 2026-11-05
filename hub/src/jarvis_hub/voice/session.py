"""Per-satellite conversation memory and spoken yes/no confirmations.

A real ESPHome satellite (through Home Assistant) may send one turn at a time, so a follow-up such
as "yes, go ahead" would arrive with nothing before it. The hub keeps each satellite's last few
turns for a short window and puts them back when the client sent no history of its own. The same
session holds a question JARVIS asked aloud ("Shall I go ahead, sir?") until the next words."""

import re
import time
from dataclasses import dataclass, field

_YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|go for it|please( do)?|proceed|"
                  r"affirmative|confirm(ed)?|carry on|send it|right|correct|absolutely|of course)\b", re.I)
# anywhere in the answer, not just at the start: "OK, don't send it" and "yes, but wait" are a no
_NO = re.compile(r"\b(no|nope|nah|don'?t|do not|cancel|stop|never ?mind|negative|leave it|forget it|"
                 r"not now|wait|hold on)\b", re.I)


def answer(text: str) -> bool | None:
    """True for a spoken yes, False for a no, None for anything else (a new request).
    Any negation wins: a wrong yes acts, a wrong no only asks to be said again."""
    if _NO.search(text):
        return False
    if _YES.match(text):
        return True
    return None


@dataclass
class PendingTool:
    """A tool call that waits for a spoken yes; picked up on the session's next turn."""

    prompt: str
    tool: str  # internal name
    args: dict
    call_id: str
    convo: list[dict]  # the conversation up to and including the assistant's tool call
    expires: float
    chain: dict | None = None  # a part of a request with several: the parts and what's done (agent/_chain)


@dataclass
class Session:
    turns: list[dict] = field(default_factory=list)
    last: float = 0.0
    started: float = 0.0  # when this conversation began (its trace session)
    pending: PendingTool | None = None


class Sessions:
    def __init__(self, window_s: float = 120, max_turns: int = 8) -> None:
        self.window_s, self.max_turns = window_s, max_turns
        self._by_key: dict[str, Session] = {}

    def _get(self, key: str) -> Session:
        s = self._by_key.setdefault(key, Session())
        if time.time() - s.last > self.window_s:  # a new conversation: forget the old one
            s.turns, s.pending, s.started = [], None, time.time()
        return s

    def merge(self, key: str, messages: list[dict]) -> list[dict]:
        """The client's messages, with this session's recent turns in front when it sent none."""
        own_history = any(m.get("role") == "assistant" for m in messages) or \
            sum(m.get("role") == "user" for m in messages) > 1
        return messages if own_history else [*self._get(key).turns, *messages]

    def started(self, key: str) -> float:
        """When this satellite's current conversation began."""
        return self._get(key).started

    def record(self, key: str, user: str, reply: str) -> None:
        s = self._get(key)
        if user:
            s.turns.append({"role": "user", "content": user})
        s.turns.append({"role": "assistant", "content": reply})
        s.turns = s.turns[-self.max_turns:]
        s.last = time.time()

    def ask(self, key: str, pending: PendingTool) -> None:
        s = self._get(key)
        s.pending, s.last = pending, time.time()

    def take(self, key: str) -> PendingTool | None:
        """The waiting question, once: it lapses whatever the answer was, or when it expires."""
        s = self._get(key)
        p, s.pending = s.pending, None
        return p if p and p.expires > time.time() else None
