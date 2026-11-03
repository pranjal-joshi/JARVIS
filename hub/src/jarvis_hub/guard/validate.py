"""Argument checks before a tool runs: the first pre-tool hook (guard/hooks.py).

A 2B model gets the shape of a call right far more often than the details: "10" for a number, "Pause"
for an enum value "pause", "the pasta timer" for a timer labelled "pasta". What can be fixed without
guessing is fixed here, and the call goes ahead with the corrected arguments (the trace says so);
anything else goes back to the model as an ERROR that says what is valid.

Resolvers check an argument against live values (running timers, notes pages). They are registered by
the code that owns those values and return one of: the corrected arguments, an ERROR string, or None
(nothing to say). A resolver only ever picks a value when exactly one fits."""

import difflib
import re
from collections.abc import Callable

from jarvis_hub.approval.gate import Risk

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())} | {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
                                                      "ninety": 90, "half": 0.5, "a": 1, "an": 1}
Resolver = Callable[[dict], "dict | str | None"]
RESOLVERS: dict[str, Resolver] = {}  # internal tool name -> resolver


def resolver(tool: str, fn: Resolver) -> None:
    RESOLVERS[tool] = fn


def coerce(schema: dict, args: dict) -> tuple[dict, list[str]]:
    """Arguments fixed to the tool's JSON schema where that's unambiguous; and the problems left."""
    props = (schema or {}).get("properties", {})
    out, problems = {}, []
    for key, value in args.items():
        spec = props.get(key)
        if value is None and key not in (schema or {}).get("required", []):
            continue  # an optional field sent as null: leave it out
        if spec is None:
            out[key] = value
            continue
        kind = spec.get("type")
        if isinstance(value, str):
            value = value.strip().strip('"').strip("'").strip()
        if kind in ("integer", "number") and isinstance(value, str):
            num = _number(value)
            if num is None:
                problems.append(f"{key} must be a number, not {value!r}")
                continue
            value = int(num) if kind == "integer" and float(num).is_integer() else num
        elif kind == "boolean" and isinstance(value, str):
            low = value.lower()
            if low in ("true", "yes", "on", "1"):
                value = True
            elif low in ("false", "no", "off", "0"):
                value = False
            else:
                problems.append(f"{key} must be true or false")
                continue
        elif kind == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if (enum := spec.get("enum")) and value not in enum:
            match = next((e for e in enum if str(e).lower() == str(value).lower()), None)
            if match is None:
                close = difflib.get_close_matches(str(value).lower(), [str(e).lower() for e in enum], n=1, cutoff=0.8)
                match = next((e for e in enum if close and str(e).lower() == close[0]), None)
            if match is None:
                problems.append(f"{key} must be one of {', '.join(map(str, enum))}")
                continue
            value = match
        out[key] = value
    return out, problems


def _number(text: str) -> float | None:
    text = text.lower().replace(",", "").strip()
    if m := re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return float(m[0])
    if m := re.match(r"(-?\d+(?:\.\d+)?)\s*[a-z%]*$", text):  # "10 minutes", "25%"
        return float(m[1])
    words = text.replace("-", " ").split()
    if words and all(w in _NUMBER_WORDS for w in words):
        return float(sum(_NUMBER_WORDS[w] for w in words))
    return None


def check(tool, args: dict) -> tuple[dict | None, str]:
    """(fixed arguments or None if unchanged, ERROR message or "")."""
    fixed, problems = coerce(tool.parameters or {}, args)
    if problems:
        return None, (f"ERROR: {tool.model_name}: " + "; ".join(problems) + ". Call it again with valid values.")
    if (fn := RESOLVERS.get(tool.name)) and tool.risk is not Risk.DESTRUCTIVE:
        res = fn(fixed)
        if isinstance(res, str):
            return None, res
        if isinstance(res, dict):
            fixed = res
    return (fixed if fixed != args else None), ""


# ---------- resolvers for live values ----------
def best_name(wanted: str, names: list[str]) -> str | None:
    """The one name `wanted` clearly means: exact (any case), contained either way ("the pasta timer" /
    "pasta"), or a single close spelling. None if none or several fit."""
    key = re.sub(r"\b(the|my|a|timer|page|list)\b", " ", wanted.lower())
    key = " ".join(key.split())
    low = {n.lower(): n for n in names}
    if wanted.lower() in low:
        return low[wanted.lower()]
    if not key:
        return names[0] if len(names) == 1 else None
    hits = [n for l, n in low.items() if key in l or (l and l in wanted.lower())]
    if len(hits) == 1:
        return hits[0]
    close = difflib.get_close_matches(key, list(low), n=2, cutoff=0.75)
    return low[close[0]] if len(close) == 1 else None
