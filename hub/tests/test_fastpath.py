"""Fast paths (answered by code, no model) and slot checks on writes."""

import asyncio
import json
from datetime import date

from jarvis_hub.agent.fastpath import FastPaths, _expression, _target_date
from jarvis_hub.agent.tool_agent import ToolAgent, _Turn
from jarvis_hub.guard.hooks import unsaid
from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.kb.graph import ObsidianVault
from jarvis_hub.mcp.registry import Tool, ToolRegistry
from jarvis_hub.tools.utility import zone_for

TODAY = date(2026, 11, 2)


def fp(tmp_path, home=""):
    g = ObsidianVault(str(tmp_path))
    g.write_jarvis_page("jarvis/location", "- User lives in Pune, Keshavnagar area.")
    return FastPaths(g, home)


def plan(f, text):
    p = f.match(text, TODAY)
    return (p.intent, p.steps) if p else None


def test_intents(tmp_path):
    f = fp(tmp_path)
    assert plan(f, "What time is it?") == ("time", [("get_time", {})])
    assert plan(f, "hey jarvis, what's the time in tokyo") == ("time", [("get_time", {"timezone": "tokyo"})])
    assert plan(f, "what's the date today") == ("date", [("get_time", {})])
    assert plan(f, "what's 17 times 23") == ("maths", [("calculate", {"expression": "17 * 23"})])
    assert plan(f, "what is 15 percent of 2,350") == ("maths", [("calculate", {"expression": "(15/100*2350)"})])
    assert plan(f, "what's the weather in Mumbai tomorrow") == ("weather", [("weather", {"place": "mumbai"})])
    assert plan(f, "what's the weather where I live") == ("weather", [("weather", {"place": "Pune"})])
    assert plan(f, "set a timer for 10 minutes for pasta") == ("timer_set", [("set_timer", {"seconds": 600.0,
                                                                                            "label": "pasta"})])
    assert plan(f, "set a pasta timer for 5 minutes")[1][0][1] == {"seconds": 300.0, "label": "pasta"}
    assert plan(f, "remind me in 20 minutes to call mum") == ("remind", [("set_timer", {"seconds": 1200.0,
                                                                                         "label": "call mum"})])
    assert plan(f, "what timers are running") == ("timer_list", [("list_timers", {})])
    assert plan(f, "cancel the pasta timer") == ("timer_cancel", [("cancel_timer", {"label": "pasta"})])
    assert plan(f, "what are my tasks") == ("tasks", [("kb_tasks", {})])
    assert plan(f, "what can you do")[0] == "help"
    for chat in ["tell me a joke", "what's the weather in Pune and the time in Tokyo", "what's the capital of France",
                 "what is love", "remember my sister's birthday is 9 June",
                 "send a notification to my laptop saying take an umbrella", "text mum it's going to rain"]:
        assert plan(f, chat) is None, chat


def test_days_until_is_computed_not_guessed(tmp_path):
    f = fp(tmp_path)
    p = f.match("how many days until 25 December", TODAY)
    assert p.say([]) == "53 days, sir, until Friday 25 December."
    assert f.match("how long until christmas", TODAY).say([]) == "53 days, sir, until Friday 25 December."
    assert _target_date("1 march", TODAY) == date(2027, 3, 1)


def test_expression_needs_an_operator():
    assert _expression("what is the capital of france") is None
    assert _expression("what's 2026") is None
    assert _expression("what's the square root of 144") == "sqrt(144)"


def test_zone_for():
    from zoneinfo import available_timezones
    if not available_timezones():  # Windows without the tzdata package; the hub's container has it
        import pytest
        pytest.skip("no time zone database here")
    assert zone_for("Tokyo") == "Asia/Tokyo" and zone_for("new york") == "America/New_York"
    assert zone_for("Pune") == "Asia/Kolkata" and zone_for("Europe/London") == "Europe/London"


def make_agent(tmp_path, script, ran):
    reg = ToolRegistry()

    def tool(name, risk=Risk.READ, out="ok", required=()):
        async def fn(args, ctx):
            ran.append((name, args))
            return out
        reg.add(Tool(name, "builtin", name, {"type": "object", "properties": {k: {"type": "string"} for k in required},
                                             "required": list(required)}, risk, fn))
    tool("get_time", out="Monday 02 November 2026, 11:40 (IST)")
    tool("kb_remember", Risk.WRITE, "Saved.", ("fact",))

    class LLM:
        model = "jarvis-lfm3-gpu"
        seen = []

        async def chat(self, messages, tools=None):
            LLM.seen.append(messages)
            return script.pop(0)
    return ToolAgent(LLM(), reg, ApprovalGate(EventBus(), 1), AuditStore(":memory:"), ask_for=frozenset(),
                     fast=fp(tmp_path))


def test_fast_path_skips_the_model(tmp_path):
    ran = []
    a = make_agent(tmp_path, [], ran)  # an empty script: any model call would fail
    reply = asyncio.run(a.run(messages=[{"role": "user", "content": "what time is it"}], source=Source.WEB,
                              request_id="r", bus=EventBus()))
    assert reply == "It's 11:40, sir." and ran == [("get_time", {})]
    assert a.audit.recent()[0]["tool"] == "get_time"  # still audited


def test_slot_check_sends_a_changed_date_back(tmp_path):
    ran = []

    def call(fact):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "kb_remember", "arguments": json.dumps({"fact": fact})}}]}
    a = make_agent(tmp_path, [call("Chiku's birthday is on 9 September"), call("Sister Chiku's birthday is 9 June"),
                              {"role": "assistant", "content": "Saved, sir."}], ran)
    asyncio.run(a.run(messages=[{"role": "user", "content": "my sister Chiku's birthday is 9 June"}],
                      source=Source.WEB, request_id="r", bus=EventBus()))
    assert ran == [("kb_remember", {"fact": "Sister Chiku's birthday is 9 June"})]
    assert any("September" in m["content"] for m in a.llm.seen[-1] if m["role"] == "tool")


def test_unsaid():
    heard = "text Chiku I'm running ten minutes late"
    send = Tool("adb_sms_send", "devices", "", {}, Risk.DESTRUCTIVE, None)
    assert unsaid(send, {"to": "Chiku", "text": "Running 10 minutes late"}, heard, []) == []
    assert unsaid(send, {"to": "Chiku", "text": "Running 15 minutes late, Rohan"}, heard, []) == ["15", "Rohan"]
    assert unsaid(Tool("weather", "b", "", {}, Risk.READ, None), {"place": "Paris"}, heard, []) == []
