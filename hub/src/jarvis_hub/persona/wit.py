"""The wit dial: how much humour JARVIS allows itself, changed by voice or the HUD.

0 plain: the answer and nothing more (guests, children, a busy morning)
1 dry:   the usual J.A.R.V.I.S. manner, dry humour now and then (the default)
2 playful: a light quip where it fits, and one more sentence to say it in

Each level swaps one style line in the system prompt, so the prompt stays the same size. A 2B model is
reliably plain at 0; at 2 it is best effort (the escalation model does better). A satellite (a room) can
have its own level, and any change can run out after a while ("for the next hour")."""

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

LEVELS = {0: "plain", 1: "dry", 2: "playful"}
STYLE = {
    0: "Style: calm and courteous, strictly to the point; no jokes, quips or asides",
    1: "Style: the manner of J.A.R.V.I.S.: British, calm, formal, dry humour",
    2: "Style: the manner of J.A.R.V.I.S. at ease: British, warm, witty; a light, kind quip when it fits, never at "
       "the expense of the answer",
}
# words a person may use for a level, or for a step up or down
NAMED = {"0": 0, "zero": 0, "off": 0, "none": 0, "plain": 0, "serious": 0, "formal": 0, "minimum": 0, "min": 0,
         "1": 1, "one": 1, "on": 1, "dry": 1, "normal": 1, "usual": 1, "default": 1, "reset": 1,
         "2": 2, "two": 2, "playful": 2, "funny": 2, "witty": 2, "max": 2, "maximum": 2, "full": 2}
STEPS = {"up": 1, "more": 1, "higher": 1, "down": -1, "less": -1, "lower": -1}


def parse(value, current: int) -> int | None:
    """A level from what was asked: 0-2, a name ("plain", "playful") or a step ("up", "less")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value) if 0 <= value <= 2 else None
    word = re.sub(r"[^a-z0-9]", "", str(value).lower())
    if word in NAMED:
        return NAMED[word]
    if word in STEPS:
        return max(0, min(2, current + STEPS[word]))
    return None


class Wit:
    """The current level: one for the house, optional ones per satellite, each with an optional end time."""

    def __init__(self, path: Path | None = None, default: int = 1) -> None:
        self.path, self.default = path, max(0, min(2, default))
        self.levels: dict[str, dict] = {}  # "" = the house; else a satellite's conversation id
        if path and path.exists():
            try:
                self.levels = {k: v for k, v in json.loads(path.read_text()).items() if v.get("level") in LEVELS}
            except Exception:
                log.exception("wit settings not read; using the default")

    def _save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.levels, indent=1))

    def _live(self, key: str) -> dict | None:
        e = self.levels.get(key)
        if e and e.get("until") and e["until"] <= time.time():
            self.levels.pop(key, None)
            self._save()
            return None
        return e

    def level(self, satellite: str = "") -> int:
        for key in ([satellite] if satellite else []) + [""]:
            if e := self._live(key):
                return e["level"]
        return self.default

    def set(self, level: int, satellite: str = "", minutes: float | None = None) -> dict:
        e = {"level": max(0, min(2, int(level)))}
        if minutes:
            e["until"] = time.time() + float(minutes) * 60
        self.levels[satellite] = e
        self._save()
        return e

    def clear(self, satellite: str = "") -> bool:
        gone = self.levels.pop(satellite, None) is not None
        self._save()
        return gone

    def state(self) -> dict:
        for key in list(self.levels):
            self._live(key)
        house = self.levels.get("")
        return {"default": self.default, "level": self.level(), "names": LEVELS,
                "until": house.get("until") if house else None,
                "satellites": {k: v for k, v in self.levels.items() if k}}


def spoken(level: int, minutes: float | None = None) -> str:
    what = {0: "Plain answers from now on", 1: "Back to my usual dry self", 2: "I'll allow myself a little more wit"}
    span = ""
    if minutes:
        m = round(float(minutes))
        span = (" for the next hour" if m == 60 else f" for the next {m // 60} hours" if m % 60 == 0
                else f" for {m} minutes")
    return what[level] + span


def register_wit_tool(reg, wit: Wit) -> None:
    """set_wit: JARVIS's own manner, so it never asks (self-scoped). "Be more serious", "dial the humour up"."""
    from jarvis_hub.approval.gate import Risk
    from jarvis_hub.mcp.registry import Tool

    async def set_wit(args: dict, ctx) -> str:
        here = bool(args.get("here_only")) and ctx.session
        key = ctx.session if here else ""
        level = parse(args.get("level", ""), wit.level(ctx.session))
        if level is None:
            return "ERROR: level must be 0 plain, 1 dry or 2 playful (or up, down). Ask the user which."
        minutes = args.get("minutes") or None
        if level == wit.default and not minutes and key:
            wit.clear(key)  # back to the house level in this room
        elif level == wit.default and not minutes:
            wit.clear("")
        else:
            wit.set(level, key, minutes)
        return spoken(level, minutes) + (" in here" if here else "") + ", sir."

    reg.add(Tool("set_wit", "builtin",
                 "Change how much humour JARVIS uses in its replies. Use for: 'be more serious', 'no jokes', 'dial the "
                 "humour up', 'wit to zero', 'be yourself again', 'be funnier for the next hour'.",
                 {"type": "object", "properties": {
                     "level": {"type": "string",
                               "description": "0 plain, 1 dry (usual), 2 playful, or up / down from now"},
                     "minutes": {"type": "number", "description": "only for this long, e.g. 60 for 'the next hour'"},
                     "here_only": {"type": "boolean", "description": "only this room's speaker ('in here')"}},
                  "required": ["level"]},
                 Risk.WRITE, set_wit, self_scoped=True, final_answer=True))
