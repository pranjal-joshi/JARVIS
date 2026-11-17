"""Opaque ids (background job ids) shown to the model as short session refs: `ref_9f3c`.

A 2B model copies a 16-character id wrongly, or takes one from a different line; a short ref that is the same for
the whole conversation it gets right. Tools register which argument takes the id and where the current ids come
from (opaque()). Their output reaches the model with each id replaced by its ref (after the audit has kept the real
output), and a ref in that argument is mapped back to the id before the call is checked, run and audited.

An unknown ref, or a raw id the user never said, is an error that names the refs to use. A raw id the user said
(read off the HUD) is theirs and goes through."""

import hashlib
import re
from collections.abc import Callable

from jarvis_hub.guard.validate import options

REF = re.compile(r"^ref_[0-9a-f]{4,12}$")
OPAQUE: dict[str, tuple[str, Callable[[], list[str]]]] = {}  # internal tool name -> (argument, current raw ids)
SHORT = 4  # outputs also show an id's first characters ("Started a background job (9f3c)")


def opaque(tools: list[str], arg: str, ids: Callable[[], list[str]]) -> None:
    for name in tools:
        OPAQUE[name] = (arg, ids)


class Refs:
    """One conversation's refs: the same id always gets the same ref."""

    def __init__(self) -> None:
        self.by_id: dict[str, str] = {}
        self.by_ref: dict[str, str] = {}

    def ref(self, raw: str) -> str:
        if raw in self.by_id:
            return self.by_id[raw]
        digest, n = hashlib.sha1(raw.encode()).hexdigest(), 4
        while f"ref_{digest[:n]}" in self.by_ref:
            n += 1
        ref = f"ref_{digest[:n]}"
        self.by_id[raw], self.by_ref[ref] = ref, raw
        return ref

    def hide(self, tool: str, output: str) -> tuple[str, int]:
        """output with every current id (and its short form) as a ref; and how many were replaced."""
        if tool not in OPAQUE:
            return output, 0
        n = 0
        for raw in sorted(_ids(tool), key=len, reverse=True):
            for form in dict.fromkeys([raw, raw[:SHORT]]):
                output, k = re.subn(rf"(?<![\w-]){re.escape(form)}(?![\w-])", self.ref(raw), output)
                n += k
        return output, n

    def resolve(self, tool: str, args: dict, said: str) -> tuple[dict | None, str, str]:
        """(arguments with the id, or None if unchanged; ERROR or ""; what happened, for the counters)."""
        if tool not in OPAQUE:
            return None, "", ""
        arg, _ = OPAQUE[tool]
        value = args.get(arg)
        if not isinstance(value, str) or not value.strip():
            return None, "", ""
        value = value.strip()
        known = ", ".join(r for r in options(value, list(self.by_ref)).split(", ") if r)
        if REF.match(value):
            if value in self.by_ref:
                return {**args, arg: self.by_ref[value]}, "", "mapped"
            if known:
                return None, f"ERROR: {value!r} is not a ref from this conversation. Use one of: {known}.", "unknown"
            return (None, f"ERROR: {value!r} is not a ref from this conversation. Call {_status(tool)} to see the refs.",
                    "unknown")
        ids = _ids(tool)
        raw = next((i for i in ids if value == i or len(value) >= SHORT and i.startswith(value)), None)
        if raw is None or re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", said or ""):
            return None, "", ""  # words ("the research job"), or an id the user said
        return None, (f"ERROR: {value!r} is a raw id. Use its ref instead: call it again with {arg} "
                      f"{self.ref(raw)}."), "raw"


def _ids(tool: str) -> list[str]:
    try:
        return [str(i) for i in OPAQUE[tool][1]() if str(i)]
    except Exception:
        return []


def _status(tool: str) -> str:
    return next((t for t in OPAQUE if t.endswith("_status")), tool)
