"""What the live HUD is fed: widget specs from a fixed kit, approval previews, and undo for a dream's changes."""

import asyncio
import json
import time

from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.approval.preview import preview
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.kb.dreaming import Dreamer
from jarvis_hub.kb.graph import ObsidianVault
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry
from jarvis_hub.tools.widget import clean, register_widget_tool, show


async def _noop(args, ctx):
    return ""


def _tool(name, origin="builtin", params=None):
    return Tool(name, origin, "", params or {}, Risk.WRITE, _noop)


# ---------- widgets ----------
def test_only_known_widgets_with_trimmed_text_reach_the_hud():
    assert clean({"widget": "html", "html": "<script>"}) is None
    w = clean({"widget": "compare", "title": "NAS", "columns": ["DS224+", "TS-264", "x", "y", "z"],
               "rows": [["price", "£299", "£339"], "not a row"], "script": "alert(1)"})
    assert w == {"widget": "compare", "title": "NAS", "columns": ["DS224+", "TS-264", "x", "y"],
                 "rows": [["price", "£299", "£339"]]}
    assert clean({"widget": "progress", "title": "t", "items": [{"label": "a", "percent": 250}]})["items"][0]["percent"] == 100
    assert clean({"widget": "list", "title": "t", "items": ["milk", "x" * 500]})["items"][1]["label"] == "x" * 120
    assert clean({"widget": "countdown", "title": "pasta"}) is None  # no due time


def test_show_widget_goes_to_the_hud_and_tells_the_model_to_answer_briefly():
    bus, reg = EventBus(), ToolRegistry()
    register_widget_tool(reg)
    got = bus.subscribe(Source.WEB)
    out = asyncio.run(reg.get("show_widget").call(
        {"widget": "list", "title": "Groceries", "items": [{"label": "milk", "value": "2"}]}, ToolCtx(bus, "web", "r1")))
    ev = got.get_nowait()
    assert ev.type == "widget" and ev.data["items"] == [{"label": "milk", "value": "2"}] and out.startswith("Shown")
    assert not show(bus, "r", {"widget": "nope"})


# ---------- approval previews ----------
def test_a_note_change_previews_as_a_diff(tmp_path):
    (tmp_path / "Errands.md").write_text("- Call the bank\n- Pick up dry cleaning\n", encoding="utf-8")
    g = ObsidianVault(str(tmp_path))
    p = preview(_tool("kb_append_to_page"), {"page": "Errands", "text": "Buy stamps"}, g)
    assert p["page"] == "Errands" and p["lines"][-1] == {"op": "+", "text": "Buy stamps"}
    p = preview(_tool("kb_delete_item"), {"page": "Errands", "match": "bank"}, g)
    assert p["lines"] == [{"op": "-", "text": "Call the bank  (Errands)"}]
    p = preview(_tool("kb_delete_page"), {"page": "Errands"}, g)
    assert [line["op"] for line in p["lines"]] == ["-", "-"]


def test_a_device_action_previews_what_it_touches():
    t = _tool("homeassistant__HassTurnOff", "homeassistant")
    p = preview(t, {"area": "Living Room", "domain": ["light"]}, None)
    assert p["kind"] == "targets" and p["action"] == "turn off" and p["items"] == ["Living Room", "light"]
    assert preview(_tool("set_timer"), {"seconds": 5}, None) is None


def test_the_preview_rides_on_the_approval_card():
    bus = EventBus()
    gate = ApprovalGate(bus, 0.05)
    got = bus.subscribe(Source.WEB)
    asyncio.run(gate.request(request_id="r", source=Source.WEB, tool="kb_delete_page", risk=Risk.DESTRUCTIVE,
                             arguments={}, preview={"kind": "diff", "page": "x", "lines": []}))
    assert got.get_nowait().data["preview"]["page"] == "x"


# ---------- dream undo ----------
def test_each_dream_change_can_be_undone_and_put_back(tmp_path):
    g = ObsidianVault(str(tmp_path))
    d = Dreamer(g, None, None, tmp_path / "dreams.json")
    d._write([("The user's boiler is serviced on Saturdays.", {"score": "0.9"}),
              ("The user's wife is Sanika.", {"score": "0.8"})])
    d.state["last"] = {"started": time.time(), "changes": [
        {"op": "added", "text": "The user's boiler is serviced on Saturdays."},
        {"op": "updated", "text": "The user's wife is Sanika.", "was": "The user's wife is Tanika.", "was_props": {}},
        {"op": "dropped", "text": "An old wifi hint.", "was_props": {"score": "0.1"}}]}
    texts = lambda: [t for t, _ in d.kept()]  # noqa: E731
    d.undo(0)
    assert "The user's boiler is serviced on Saturdays." not in texts()
    d.undo(1)
    assert "The user's wife is Tanika." in texts() and "The user's wife is Sanika." not in texts()
    d.undo(2)
    assert "An old wifi hint." in texts()
    d.undo(0)  # put back
    assert "The user's boiler is serviced on Saturdays." in texts()
    assert json.loads((tmp_path / "dreams.json").read_text())["last"]["changes"][0]["undone"] is False
