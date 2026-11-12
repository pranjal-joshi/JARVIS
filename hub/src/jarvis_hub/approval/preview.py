"""What an approval card shows besides the words: the effect, so a person can judge it at a glance.

A note change becomes a small diff (lines kept, added, removed), a device action a list of what it touches, a
skill its steps. Built from the arguments and the vault as they are now; nothing here runs the tool. A preview
that can't be made is None and the card shows the words alone."""

import logging
import re

log = logging.getLogger(__name__)
MAX_LINES = 8


def _lines(page, limit: int = 3) -> list[dict]:
    return [{"op": " ", "text": b.text.splitlines()[0][:160]} for b in (page.blocks if page else [])[-limit:]]


def _matching(graph, match: str, page: str = "") -> list[tuple[str, str]]:
    words = [w for w in re.findall(r"[a-z0-9]+", match.lower()) if len(w) > 1]
    out = []
    for p in ([graph.page(page)] if page else graph.pages()):
        for b in p.blocks if p else []:
            low = b.text.lower()
            if words and all(w in low for w in words):
                out.append((p.name, b.text.splitlines()[0][:160]))
    return out


def preview(tool, args: dict, graph=None) -> dict | None:
    try:
        return _preview(tool, args, graph)
    except Exception:
        log.exception("no preview for %s", tool.name)
        return None


def _preview(tool, args: dict, graph) -> dict | None:
    name, a = tool.name, args or {}
    if graph is not None and name == "kb_append_to_page":
        page = graph.page(str(a.get("page", "")))
        lines = _lines(page) + [{"op": "+", "text": str(a.get("text", ""))[:160]}]
        return {"kind": "diff", "page": page.name if page else f"{a.get('page')} (new page)", "lines": lines}
    if graph is not None and name in ("kb_delete_item", "kb_move_item"):
        hits = _matching(graph, str(a.get("match", "")), str(a.get("page") or a.get("from_page") or ""))
        lines = [{"op": "-", "text": f"{t}  ({p})"} for p, t in hits[:MAX_LINES]]
        if name == "kb_move_item":
            lines += [{"op": "+", "text": f"{t}  ({a.get('to_page')})"} for _, t in hits[:MAX_LINES]]
        more = len(hits) - MAX_LINES
        return {"kind": "diff", "page": str(a.get("to_page") or a.get("page") or "your notes"),
                "lines": lines or [{"op": " ", "text": "nothing matches now"}],
                **({"more": more} if more > 0 else {})}
    if graph is not None and name in ("kb_delete_page", "kb_forget"):
        page = graph.page(str(a.get("page", "")))
        if not page:
            return None
        lines = [{"op": "-", "text": b.text.splitlines()[0][:160]} for b in page.blocks[:MAX_LINES]]
        return {"kind": "diff", "page": page.name, "lines": lines,
                **({"more": len(page.blocks) - MAX_LINES} if len(page.blocks) > MAX_LINES else {})}
    if name in ("create_skill", "update_skill"):
        steps = str(a.get("instructions") or a.get("add_steps") or "").splitlines()
        return {"kind": "diff", "page": f"skill: {a.get('name', '')}",
                "lines": [{"op": "+", "text": s.strip()[:160]} for s in steps if s.strip()][:MAX_LINES]
                + ([{"op": " ", "text": f"when: {a.get('trigger') or a.get('schedule')}"}]
                   if a.get("trigger") or a.get("schedule") else [])}
    if tool.origin == "homeassistant":
        targets = [str(v) for k, v in a.items() if k in ("name", "area", "floor", "domain", "device_class")
                   and v not in (None, "", [])]
        targets += [str(x) for k in ("name", "area", "domain") if isinstance(a.get(k), list) for x in a[k]]
        action = re.sub(r"^Hass", "", tool.model_name.split("__")[-1])
        action = re.sub(r"(?<!^)(?=[A-Z])", " ", action).lower()
        return {"kind": "targets", "action": action,
                "items": list(dict.fromkeys(t for t in targets if not t.startswith("[")))[:MAX_LINES]
                or ["(no target named: everything it applies to)"]}
    return None
