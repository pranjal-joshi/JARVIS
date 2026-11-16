"""Harness telemetry: how often each guard, nudge, fast path, selector rule, resolver, escalation and recovery
fires, per turn (kept in the turn's trace) and in total (kept in the traces database, with when each last fired).

The point is pruning: a check that never fires on real use is a candidate to remove, and one that fires on every
turn is worth a look too. Counting never changes what a turn does and never fails it.

Names are dotted: `hook.slots`, `nudge.claim`; a detail may follow (`nudge.claim.timer`, `fastpath.hit.time`).
KNOWN lists the mechanisms by their top-level name so that ones that never fired show up with a count of 0."""

import logging
import time
from collections import Counter

log = logging.getLogger(__name__)

# name -> what it counts. Details (a hook's verdict, a fast path's intent) are counted as well, under name.detail.
KNOWN: dict[str, str] = {
    # pre-tool hooks (guard/hooks.py): any verdict; .error / .ask / .fix say which
    "hook.arguments": "argument types, enum spelling, live names fixed or refused",
    "hook.needless_search": "web search refused for general knowledge",
    "hook.needless_time": "get_time refused when the question isn't about time",
    "hook.clock_time": "timer/reminder time and day taken from the user's words",
    "hook.keep_links": "link the user gave added to a saved note",
    "hook.once": "a second note write of the same fact refused",
    "hook.own_words": "note write with words nobody said refused",
    "hook.slots": "write with names/numbers/months the user didn't say refused",
    "hook.taint": "change carrying outside text held for a human",
    "hook.after_outside_text": "change after reading outside text held for a human",
    # resolvers (guard/validate.py)
    "resolver": "a live-value resolver ran (.fixed / .error / .none and .<tool>)",
    # tool selection (agent/tool_select.py)
    "wants.url": "a link in the request offered fetch_page and web_search",
    "wants.web": "web words offered web_search",
    "wants.search": "'search' (not the notes) offered web_search",
    "wants.remember": "'remember' offered remember",
    "recall": "'remind me what...' kept timers out and offered search_notes",
    "named": "an expert or job tool offered because the user named it",
    # fast paths (agent/fastpath.py)
    "fastpath.hit": "request answered by code (.intent)",
    "fastpath.fallback": "fast path matched but a step failed: the model answered (.intent)",
    # routes and hints (agent/tool_agent.py)
    "capture": "memory capture saved facts the user stated",
    "look_first": "notes searched before the model answered",
    "bare_save": "'remember it' saved what came before",
    "explicit_web": "web search asked for by name",
    "hide.get_time": "get_time hidden: not about time",
    "hide.web_search": "web_search hidden: textbook question",
    "route_note": "a note write sent where the user said (page, journal)",
    "plan.split": "request split into parts",
    "plan.restated": "a part only restated an earlier part",
    # nudges: one more model call with a (system) note
    "nudge.fake": "model wrote tool markup as text: asked again",
    "nudge.claim": "reply claimed an action no tool did: asked again (.kind)",
    "nudge.empty": "empty reply: asked again",
    "nudge.part": "a part talked ahead of later parts: asked again",
    # detections and recoveries
    "fake.detected": "tool markup written as text",
    "pick": "constrained tool pick after a skipped tool (.claim / .must)",
    "pick.none": "constrained pick found no tool",
    "repeat": "the same call again this turn",
    "escalate": "turn sent to the bigger model (.reason)",
    "undone": "a claimed action replaced with what is true",
    "circles": "step budget ran out",
    "fake.failed": "tool markup as text twice: gave up",
}


def hit(t, name: str, detail: str = "") -> None:
    """Count one firing on a turn (anything with a `hits` list). Never raises."""
    try:
        hits = getattr(t, "hits", None)
        if hits is None:
            return
        hits.append(name)
        if detail:
            hits.append(f"{name}.{detail}")
    except Exception:  # pragma: no cover - counting must never fail a turn
        log.exception("harness hit not counted")


class HarnessStats:
    """Totals across turns: in the traces database when there is one (kept across restarts), else in memory."""

    def __init__(self, traces=None) -> None:
        self.traces = traces
        self._mem: dict[str, dict] = {}

    def add(self, hits: list[str]) -> None:
        if not hits:
            return
        counts, now = Counter(hits), time.time()
        try:
            if self.traces is not None:
                self.traces.add_hits(counts, now)
                return
        except Exception:
            log.exception("harness hits not stored")
        for name, n in counts.items():
            row = self._mem.setdefault(name, {"count": 0, "last": 0.0})
            row["count"] += n
            row["last"] = now

    def snapshot(self) -> dict:
        rows: dict[str, dict] = {}
        try:
            rows = self.traces.hit_counts() if self.traces is not None else {k: dict(v) for k, v in self._mem.items()}
        except Exception:
            log.exception("harness hits not read")
        out = []
        for name in sorted(set(rows) | set(KNOWN)):
            row = rows.get(name, {"count": 0, "last": None})
            top = next((k for k in KNOWN if name == k or name.startswith(k + ".")), "")
            out.append({"name": name, "count": row["count"], "last": row["last"] or None,
                        "about": KNOWN.get(name, ""), "known": top})
        return {"counters": out, "never_hit": [k for k in KNOWN if not rows.get(k, {}).get("count")]}
