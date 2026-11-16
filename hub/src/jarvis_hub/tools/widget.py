"""Widgets on the HUD, shaped by the request: a forecast strip, a comparison table, a list, a few numbers.

The model never writes markup. It (or a tool such as weather) sends a small spec naming one of a fixed set of
widget kinds and its data; the HUD draws it from its own components and shows every value as plain text. So
text read from a page or a note can fill a widget's fields but can't add code, and a small model only has a
short schema to fill. Specs are checked and trimmed here before they reach the HUD."""

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

KINDS = ("list", "compare", "stats", "progress", "forecast", "countdown")
MAX_ITEMS, MAX_COLS, MAX_TEXT = 8, 4, 120


def _s(v, n: int = MAX_TEXT) -> str:
    return " ".join(str(v if v is not None else "").split())[:n]


def clean(spec: dict) -> dict | None:
    """A spec the HUD can draw, or None. Only known kinds and fields, strings trimmed, numbers bounded."""
    kind = spec.get("widget")
    if kind not in KINDS:
        return None
    out = {"widget": kind, "title": _s(spec.get("title"), 80)}
    if kind == "countdown":
        try:
            out["due"] = float(spec["due"])
        except (KeyError, TypeError, ValueError):
            return None
        out["seconds"] = max(1.0, float(spec.get("seconds") or 60))
        return out
    if kind == "compare":
        cols = [_s(c, 40) for c in (spec.get("columns") or [])][:MAX_COLS]
        rows = [[_s(c, 60) for c in r][:len(cols) + 1] for r in (spec.get("rows") or []) if isinstance(r, list)]
        if not cols or not rows:
            return None
        return out | {"columns": cols, "rows": rows[:MAX_ITEMS]}
    if kind == "forecast":
        days = []
        for d in (spec.get("days") or [])[:7]:
            try:
                days.append({"label": _s(d.get("label"), 12), "low": round(float(d["low"])),
                             "high": round(float(d["high"])), "rain": int(d.get("rain") or 0),
                             "sky": _s(d.get("sky"), 30)})
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return out | {"days": days} if days else None
    items = []
    for it in (spec.get("items") or [])[:MAX_ITEMS]:
        if isinstance(it, dict):
            item = {"label": _s(it.get("label"), 60), "value": _s(it.get("value"), 60)}
            if kind == "progress":
                try:
                    item["percent"] = max(0, min(100, round(float(it.get("percent", 0)))))
                except (TypeError, ValueError):
                    item["percent"] = 0
            items.append(item)
        elif kind == "list":
            items.append({"label": _s(it), "value": ""})
    return out | {"items": items} if items else None


def show(bus, request_id: str, spec: dict) -> bool:
    """Put a widget on the HUD (and the phone's HUD). False: the spec wasn't one the HUD can draw."""
    if not bus or not (w := clean(spec)):
        return False
    for src in (Source.WEB, Source.PHONE):
        bus.publish(Event("widget", src, request_id, w))
    return True


def register_widget_tool(reg: ToolRegistry) -> None:
    async def show_widget(args: dict, ctx: ToolCtx) -> str:
        if not show(ctx.bus, ctx.request_id, args):
            return ("ERROR: the widget is incomplete. Call show_widget again with widget (list, compare, stats or "
                    "progress), a title, and items [{label, value}], or columns and rows for compare.")
        return "Shown on the HUD. Now answer in a sentence; don't repeat the table."

    reg.add(Tool("show_widget", "builtin",
                 "Show a small table or list on the HUD screen next to your answer. Use for comparisons ('compare "
                 "the DS224+ and TS-264'), lists of several items, or a few numbers side by side. Not for one fact.",
                 {"type": "object", "properties": {
                     "widget": {"type": "string", "enum": ["list", "compare", "stats", "progress"]},
                     "title": {"type": "string"},
                     "items": {"type": "array", "description": "for list, stats, progress",
                               "items": {"type": "object", "properties": {
                                   "label": {"type": "string"}, "value": {"type": "string"},
                                   "percent": {"type": "number"}}}},
                     "columns": {"type": "array", "items": {"type": "string"},
                                 "description": "compare: the things compared"},
                     "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                              "description": "compare: [feature, value for each column]"}},
                  "required": ["widget", "title"]},
                 Risk.READ, show_widget))
