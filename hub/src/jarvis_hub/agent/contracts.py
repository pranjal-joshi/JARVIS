"""Before-done contracts: the tools a turn must have run, successfully, before a text reply may end it.

A 2B model often answers "Sure, sir." to "remember it" or "text Mum I'm late" and calls nothing. The claim check
(agent/claims.py) only catches replies that *say* it was done; a contract catches the silent skip, because what has
to happen is known from the request itself, by code:

  explicit_web  "search ... on github", "do a web search"       -> web_search
  save          "remember it" (BARE_SAVE), "remember that ..."  -> a note write
  part          one part of a request with several, by its verb -> a tool of that kind (timer, message, notes, device)
  fastpath      a fast path for an action matched but a step failed -> that action's tool
  skill         `mandatory_tools` in a skill page's frontmatter (hand-written only; the skill tools don't write it)

Only calls that ran and succeeded count: not errors, refusals, dry-run stand-ins or denials. A denial, a timeout or
a policy block *closes* the contract instead: the user's (or the policy's) no is never pushed past. Contracts are
recorded but not enforced in dry runs (nothing succeeds there), and not for models without schema-constrained
output (Claude), where the forced call isn't available."""

import logging
import re
from collections import Counter
from dataclasses import dataclass

from jarvis_hub.agent.claims import KINDS
from jarvis_hub.approval.gate import Risk

log = logging.getLogger(__name__)
ON_MISSING = ("force", "undone", "ignore")
NOTE_SAVES = frozenset({"kb_remember", "kb_journal", "kb_append_to_page"})
UNDONE = {"web": "I haven't actually searched the web, sir.",
          "notes": "I haven't actually saved anything, sir. Say \"remember ...\" and I will.",
          "timer": "I haven't actually set a timer or reminder, sir.",
          "message": "I haven't actually sent anything, sir.",
          "device": "I haven't actually changed anything, sir.",
          "any": "I haven't actually done that, sir."}

_LEAD = r"^\W*(?:please\s+|jarvis,?\s+|also\s+|then\s+|and\s+|now\s+)*"
# a part's verb says what has to happen; questions and "tell me / show me" ask for an answer, not an action
PART_KINDS = (
    ("timer", re.compile(_LEAD + r"(?:(?:set|start|put on)\b.*\b(?:timer|alarm|reminder)\b|remind me\b"
                                 r"(?!\s+(?:what|who|whose|where|which|how)\b))", re.I)),
    ("message", re.compile(_LEAD + r"(?:text|message|whatsapp|send|notify|ping|call)\b", re.I)),
    ("notes", re.compile(_LEAD + r"(?:remember|note|save|jot|journal|log|add\b.*\bto (?:my |the )?(?:notes|journal|"
                                 r"page)\b)", re.I)),
    ("device", re.compile(_LEAD + r"(?:turn|switch|pause|resume|skip|lock|unlock|mute|unmute|launch|dim|brighten|"
                                  r"raise|lower|play)\b", re.I)),
)


@dataclass
class Need:
    tools: frozenset[str]  # internal tool names; a successful call to any of them counts
    why: str  # explicit_web | save | part | fastpath | skill
    kind: str = "any"  # for the truthful reply when it isn't met (UNDONE)
    min_calls: int = 1
    on_missing: str = "force"  # force (one constrained call, then the truth) | undone (the truth) | ignore
    base: int = 0  # successful calls of these tools before this need started (a part counts its own)

    def done(self, succeeded: Counter) -> int:
        return sum(succeeded[n] for n in self.tools) - self.base

    def met(self, succeeded: Counter) -> bool:
        return self.done(succeeded) >= self.min_calls

    def closed(self, declined: set) -> bool:
        return bool(self.tools & declined)

    @property
    def undone(self) -> str:
        return UNDONE.get(self.kind, UNDONE["any"])

    def view(self) -> dict:
        return {"tools": sorted(self.tools), "why": self.why, "min_calls": self.min_calls,
                "on_missing": self.on_missing}


def part_need(step: str, offered) -> Need | None:
    """What one part of a request with several must do, by its verb; None for a question or a request for words."""
    if re.search(r"\?\s*$", step):
        return None
    for kind, rx in PART_KINDS:
        if not rx.search(step):
            continue
        if kind == "device":  # whatever offered tool changes something (Home Assistant, media keys, the laptop)
            tools = frozenset(t.name for t in offered if t.risk is not Risk.READ)
        elif kind == "notes":
            tools = NOTE_SAVES
        else:
            tools = KINDS[kind]
        return Need(tools, "part", kind) if tools else None
    return None


def skill_needs(raw, allowed) -> list[Need]:
    """`mandatory_tools` from a skill's frontmatter: [{name, min_calls, on_missing}]. Names must be tools this run
    may use (allowed: internal name -> Tool); anything else is dropped with a warning."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        tool = allowed(str(item.get("name") or ""))
        try:
            n = max(1, min(int(item.get("min_calls") or 1), 5))
        except (TypeError, ValueError):
            n = 1
        how = str(item.get("on_missing") or "force").lower()
        if tool is None or how not in ON_MISSING:
            log.warning("skill mandatory_tools entry ignored: %r", item)
            continue
        out.append(Need(frozenset({tool.name}), "skill", "any", n, how))
    return out
