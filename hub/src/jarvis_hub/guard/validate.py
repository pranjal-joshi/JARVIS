"""Argument checks before a tool runs: the first pre-tool hook (guard/hooks.py).

A 2B model gets the shape of a call right far more often than the details: "10" for a number, "Pause"
for an enum value "pause", "the pasta timer" for a timer labelled "pasta". What can be fixed without
guessing is fixed here, and the call goes ahead with the corrected arguments (the trace says so);
anything else goes back to the model as an ERROR that says what is valid.

Resolvers check an argument against live values (running timers, notes pages). They are registered by
the code that owns those values and return one of: the corrected arguments, an ERROR string, or None
(nothing to say). A resolver only ever picks a value when exactly one fits."""

import copy
import difflib
import inspect
import re
from collections.abc import Callable

from jarvis_hub.approval.gate import Risk

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())} | {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
                                                      "ninety": 90, "half": 0.5, "a": 1, "an": 1}
Resolver = Callable[[dict], "dict | str | None"]
RESOLVERS: dict[str, Resolver] = {}  # internal tool name -> resolver


# An error the model gets back says what to do next, in a sentence that starts with one of these (tested over the
# whole source: tests/test_harness.py). Where the valid values are few, it lists the nearest five (options()).
ACTIONABLE = re.compile(r"(?:^|[.;:!?)]\s+)(?:Call|Ask|Answer|Tell|Use|Say|Give|Don't|Do not|Try|Pick|Search|Save|"
                        r"Wait|Make|Check)\b")
NEXT_STEP = " Tell the user briefly that it failed and why, in plain words; don't call it again with the same arguments."


def actionable(output: str) -> str:
    """A tool's ERROR with a next step: one whose text doesn't say (an exception, an MCP server's error) gets the
    plain one. Anything else is returned as it is."""
    if not output.startswith("ERROR") or ACTIONABLE.search(output):
        return output
    return output.rstrip().rstrip(".") + "." + NEXT_STEP


def options(wanted: str, choices, n: int = 5) -> str:
    """The n choices closest to wanted, nearest first, as "a, b, c": what an error lists as valid."""
    choices = [str(c) for c in dict.fromkeys(choices)]
    w = str(wanted or "").lower()

    def score(c: str) -> float:
        low = c.lower()
        return difflib.SequenceMatcher(None, w, low).ratio() + (0.5 if w and (w in low or low in w) else 0)
    return ", ".join(sorted(choices, key=lambda c: -score(c))[:n])


# ---------- live enums: valid values that change (running timers, pages, skills, Home Assistant's devices) ----------
LIVE_CAP = 30  # more values than this: no enum (a long list costs context); the resolver handles it
LiveFn = Callable[..., "list[str] | object"]  # () or (turn) -> values, or an awaitable of them
LIVE: dict[str, dict[str, tuple[LiveFn, bool]]] = {}  # internal tool name -> arg -> (values, strict)


def live_enum(tool: str, arg: str, fn: LiveFn, strict: bool = True) -> None:
    """Offer arg's current values as a JSON-schema enum. strict: a value outside them is an error (the list is
    the truth: running timers); not strict: it goes through as it is (a cached list that may be out of date)."""
    LIVE.setdefault(tool, {})[arg] = (fn, strict)


async def live_values(fns: dict[str, tuple[LiveFn, bool]], turn=None) -> dict[str, tuple[list[str], bool]]:
    """{arg: (values, strict)} for the args with 1..LIVE_CAP current values."""
    out = {}
    for arg, (fn, strict) in fns.items():
        try:
            got = fn(turn) if len(inspect.signature(fn).parameters) else fn()
            if inspect.isawaitable(got):
                got = await got
        except Exception:  # a value source that fails leaves the argument as it was
            continue
        values = [str(v) for v in dict.fromkeys(got or []) if str(v).strip()]
        if 0 < len(values) <= LIVE_CAP:
            out[arg] = (values, strict)
    return out


def with_enums(schema: dict, live: dict[str, tuple[list[str], bool]]) -> dict:
    """An OpenAI tool schema with live enums added (a copy; the tool's own schema is never changed)."""
    props = ((schema.get("function") or {}).get("parameters") or {}).get("properties") or {}
    usable = {a: v for a, (v, _) in live.items() if props.get(a, {}).get("type", "string") == "string"}
    if not usable:
        return schema
    out = copy.deepcopy(schema)
    for arg, values in usable.items():
        out["function"]["parameters"]["properties"].setdefault(arg, {"type": "string"})["enum"] = values
    return out


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
                problems.append(f"{key} must be one of {options(str(value), enum)}")
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


def check(tool, args: dict, hits: list | None = None,
          live: dict[str, tuple[list[str], bool]] | None = None) -> tuple[dict | None, str]:
    """(fixed arguments or None if unchanged, ERROR message or ""). hits: where to count resolver outcomes.
    live: {arg: (values, strict)} the model was shown as an enum this turn (live_values): a value of another case
    is fixed; one not among them goes to the resolver, and if that doesn't place it, a strict one is an error."""
    fixed, problems = coerce(tool.parameters or {}, args)
    if problems:
        return None, (f"ERROR: {tool.model_name}: " + "; ".join(problems) + ". Call it again with valid values.")
    missed = []
    for arg, (values, strict) in (live or {}).items():
        value = fixed.get(arg)
        if not isinstance(value, str) or value in values:
            continue
        if (same := next((v for v in values if v.lower() == value.strip().lower()), None)) is not None:
            fixed[arg] = same
            if hits is not None:
                hits += ["live", "live.fixed"]
            continue
        missed.append((arg, values, strict, value))
    placed = False  # a resolver recognised the value (and may have kept it as it was)
    if (fn := RESOLVERS.get(tool.name)) and tool.risk is not Risk.DESTRUCTIVE:
        res = fn(fixed)
        placed = isinstance(res, dict)
        if hits is not None:
            outcome = "error" if isinstance(res, str) else "fixed" if isinstance(res, dict) and res != fixed else "none"
            hits += ["resolver", f"resolver.{outcome}", f"resolver.{tool.name}"]
        if isinstance(res, str):
            return None, res
        if isinstance(res, dict):
            fixed = res
    for arg, values, strict, value in missed:
        if placed or fixed.get(arg) != value or not strict:  # the resolver placed it, or the list may be stale
            continue
        if hits is not None:
            hits += ["live", "live.error"]
        return None, (f"ERROR: {arg} {value!r} isn't one of the current values. Closest: {options(value, values)}. "
                      f"Call {tool.model_name} again with one of those, or ask the user which.")
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
