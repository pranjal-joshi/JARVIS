"""Small-model tool handling: spoken names, hidden tools, per-request selection, errors that say what next."""

import asyncio
import json

from jarvis_hub.agent.tool_agent import ToolAgent
from jarvis_hub.agent.tool_select import ToolSelector
from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.kb.graph import LogseqGraph
from jarvis_hub.kb.tools import missing_page
from jarvis_hub.llm.client import LLMError
from jarvis_hub.mcp.registry import Tool, ToolRegistry

TOPICS = ["time", "maths", "notes", "text", "weather", "timer", "laptop", "call", "tap"]


class FakeEmbedder:
    """One dimension per topic word: a text's vector counts the topic words in it."""

    def __init__(self, fail=False):
        self.fail = fail

    async def embed(self, texts, model):
        if self.fail:
            raise LLMError("down")
        return [[t.lower().count(w) + 0.01 for w in TOPICS] for t in texts]


class ToolsLLM:
    model = "jarvis-2b-gpu"

    def __init__(self, script):
        self.script, self.offered = list(script), []

    async def chat(self, messages, tools=None):
        self.offered.append(sorted(t["function"]["name"] for t in tools or []))
        return self.script.pop(0)


def call(tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}


def registry(ran):
    reg = ToolRegistry()

    def tool(name, desc, risk=Risk.READ, required=()):
        async def fn(args, ctx):
            ran.append((name, args))
            return f"{name} ok"
        props = {k: {"type": "string"} for k in required}
        reg.add(Tool(name, "builtin", desc, {"type": "object", "properties": props, "required": list(required)},
                     risk, fn))

    tool("get_time", "time now")
    tool("calculate", "maths")
    tool("kb_search", "search notes")
    tool("adb_sms_send", "send a text message", Risk.WRITE, ("to", "text"))
    tool("weather", "weather forecast")
    tool("set_timer", "start a timer")
    tool("win_lock", "lock the laptop")
    tool("adb_call", "phone call someone")
    tool("adb_tap", "tap the phone screen")
    tool("list_timers", "running timer list")
    return reg


def agent(script, ran, k=2, fail=False):
    return ToolAgent(ToolsLLM(script), registry(ran), ApprovalGate(EventBus(), 2),
                     AuditStore(":memory:"), ask_for=frozenset(), hidden=frozenset({"adb_tap"}),
                     selector=ToolSelector(FakeEmbedder(fail), "m", k))


def ask(a, text):
    return asyncio.run(a.run(messages=[{"role": "user", "content": text}], source=Source.WEB,
                             request_id="r1", bus=EventBus()))


def test_small_models_get_core_plus_matching_tools_by_spoken_name():
    ran = []
    a = agent([{"role": "assistant", "content": "Fine, sir."}], ran)
    ask(a, "send a text to mum, and what's the weather")
    # make_call comes with send_text: the same family (tools/cards.py), so the model picks between them;
    # get_time only when the request is about time (the time rides on every message anyway)
    assert a.llm.offered[0] == ["calculate", "make_call", "search_notes", "send_text", "weather"]
    a = agent([{"role": "assistant", "content": "Fine, sir."}], ran)
    ask(a, "what's the weather this evening")
    assert "get_time" in a.llm.offered[0]


def test_selection_falls_back_to_every_visible_tool_and_claude_sees_all():
    ran = []
    a = agent([{"role": "assistant", "content": "ok"}], ran, fail=True)
    ask(a, "send a text")
    assert len(a.llm.offered[0]) == 8 and "phone_tap" not in a.llm.offered[0]  # all visible but get_time
    a = agent([{"role": "assistant", "content": "ok"}], ran)
    a.llm.model = "claude-haiku-5-5"
    ask(a, "send a text")
    assert len(a.llm.offered[0]) == 8


def test_spoken_name_runs_the_tool_and_audit_keeps_the_internal_name():
    ran = []
    a = agent([call("send_text", to="Mum", text="hi"), {"role": "assistant", "content": "Sent, sir."}], ran)
    assert ask(a, "text mum hi") == "Sent, sir."
    assert ran == [("adb_sms_send", {"to": "Mum", "text": "hi"})]
    assert a.audit.recent()[0]["tool"] == "adb_sms_send"


def test_errors_say_what_to_do_next():
    ran = []
    a = agent([], ran)

    def result(tool, **args):
        return asyncio.run(a._run_tool(call(tool, **args)["tool_calls"][0], Source.WEB, "r", EventBus()))

    assert "switched off" in result("phone_tap", x="1")
    assert "Did you mean: send_text" in result("send_txt", to="Mum")
    missing = result("send_text", to="Mum")
    assert missing.startswith("ERROR: send_text needs text.") and '{"to": "...", "text": "..."}' in missing
    assert ran == []  # hidden, unknown and incomplete calls never run


def test_missing_page_names_the_closest_pages(tmp_path):
    g = LogseqGraph(str(tmp_path))
    g.append_to_user_page("Groceries", "milk")
    g.write_jarvis_page("jarvis/preferences", "tea")
    assert "Closest: Groceries" in missing_page(g, "Grocery", "read_page")
    assert "jarvis/preferences" in missing_page(g, "preferences", "forget_memory_page")
    assert "search_notes" in missing_page(g, "Holiday plans", "read_page")


def test_remember_is_offered_and_remind_me_what_is_a_question():
    reg = registry([])
    reg.add(Tool("kb_remember", "builtin", "save a fact", {"type": "object", "properties": {}}, Risk.WRITE, None))
    sel = ToolSelector(FakeEmbedder(False), "m", 1)

    def offered(say):
        return {t.name for t in asyncio.run(sel.rank(reg.all(), [{"role": "user", "content": say}])).tools}
    # asked to keep something: the memory tool, whatever the embeddings matched
    assert "kb_remember" in offered("also remember that we need a nightly job which looks at the conversations")
    # "remind me what I'm called" asks the notes, so the timer and reminder tools stay out
    got = offered("remind me what I'm called again")
    assert "kb_search" in got and not got & {"set_timer", "list_timers"}
