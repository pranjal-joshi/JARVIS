"""Voice-only use: satellite memory, spoken yes/no, approval read-back, background answers,
announcements, listening style and misheard names."""

import asyncio
import json

from jarvis_hub.agent.tool_agent import ToolAgent
from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Settings, Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.kb.graph import ObsidianVault
from jarvis_hub.mcp.registry import Tool, ToolRegistry
from jarvis_hub.voice.announce import Announcer
from jarvis_hub.voice.session import Sessions, answer
from jarvis_hub.voice.speakable import for_ear
from jarvis_hub.voice.vocab import Vocabulary

SAT = "satellite:default"


class FakeLLM:
    model = "jarvis-2b-gpu"

    def __init__(self, script):
        self.script, self.seen = list(script), []

    async def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        return self.script.pop(0)


def call(tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


def setup(script, risk=Risk.EXTERNAL, final=False):
    ran, reg = [], ToolRegistry()

    async def fn(args, ctx):
        ran.append(args)
        return "Forty-two." if final else "Pune: 24 C, clear"

    reg.add(Tool("web_search", "expert" if final else "web", "search", {"type": "object", "properties": {"query": {"type": "string"}},
                                                  "required": ["query"]}, risk, fn, final_answer=final))
    bus = EventBus()
    agent = ToolAgent(FakeLLM(script), reg, ApprovalGate(bus, 0.05), AuditStore(":memory:"),
                      sessions=Sessions(), voice_confirm=frozenset({Risk.EXTERNAL}))
    return agent, bus, ran


def turn(agent, bus, text, source=Source.SATELLITE, cues=None):
    return asyncio.run(agent.run(messages=[{"role": "user", "content": text}], source=source, request_id="r",
                                 bus=bus, on_event=(cues.append if cues is not None else None),
                                 session=SAT if source is Source.SATELLITE else None))


def test_yes_no_words():
    assert answer("Yes please") and answer("go ahead, sir") and answer("ok")
    assert answer("no") is False and answer("don't") is False and answer("No, do it") is False
    assert answer("what's the weather") is None
    for hedged in ["OK, don't send it", "yes but wait", "sure, no thanks", "go ahead, actually hold on"]:
        assert answer(hedged) is False, hedged


def test_session_adds_recent_turns_only_for_single_turn_clients():
    s = Sessions()
    s.record(SAT, "text Chiku", "What should it say, sir?")
    one = [{"role": "user", "content": "on my way"}]
    assert [m["content"] for m in s.merge(SAT, one)] == ["text Chiku", "What should it say, sir?", "on my way"]
    own = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, *one]
    assert s.merge(SAT, own) == own


def test_spoken_yes_runs_the_external_tool_and_carries_on():
    agent, bus, ran = setup([call("web_search", query="Pune weather"), say("Clear and 24 degrees, sir.")])
    assert turn(agent, bus, "search the web for the weather in Pune") == "That will search the web for Pune weather. Shall I go ahead, sir?"
    assert ran == []
    assert turn(agent, bus, "yes") == "Clear and 24 degrees, sir."
    assert ran == [{"query": "Pune weather"}]
    row = agent.audit.recent()[0]
    assert (row["status"], row["approved_by"]) == ("ok", "voice")
    assert agent.llm.seen[-1][-1]["role"] == "tool"  # the model saw the result of the confirmed call


def test_spoken_no_leaves_it_and_anything_else_is_a_new_request():
    agent, bus, ran = setup([call("web_search", query="x"), call("web_search", query="y"), say("Hello, sir.")])
    turn(agent, bus, "search x")
    assert turn(agent, bus, "no thanks") == "Very well, sir. I've left it."
    assert ran == [] and agent.audit.recent()[0]["status"] == "denied"
    turn(agent, bus, "search y")
    assert turn(agent, bus, "hello there") == "Hello, sir."  # the question lapsed; nothing ran
    assert ran == []


def test_the_hud_is_not_asked_by_voice_and_destructive_waits_with_read_back():
    agent, bus, ran = setup([call("web_search", query="x"), say("Not approved, sir.")], risk=Risk.DESTRUCTIVE)
    agent.ask_for = frozenset({Risk.DESTRUCTIVE})
    cues = []
    assert turn(agent, bus, "search for it now", cues=cues) == "Not approved, sir."  # gate timed out
    assert cues[0] == {"say": "Please approve on your phone or the HUD: search the web for x."}
    assert ran == []


def test_slow_expert_answers_are_announced_not_waited_for():
    agent, bus, ran = setup([call("web_search", query="q")], final=True)
    agent.ask_for = frozenset()
    got = bus.subscribe(Source.SATELLITE)

    async def scenario():
        reply = await agent.run(messages=[{"role": "user", "content": "look up the latest on it"}], source=Source.SATELLITE,
                                request_id="r", bus=bus, session=SAT)
        while True:
            ev = await asyncio.wait_for(got.get(), 2)
            if ev.type == "announce":
                return reply, ev.data
    reply, data = asyncio.run(scenario())
    assert reply.startswith("Working on it") and data == {"text": "Forty-two.", "kind": "answer"}
    assert agent.sessions.merge(SAT, [])[-1]["content"] == "Forty-two."  # follow-ups can refer to it


def test_web_requests_are_unchanged():
    agent, bus, ran = setup([call("web_search", query="x"), say("ok")])
    agent.ask_for = frozenset()
    assert turn(agent, bus, "search", source=Source.WEB) == "ok." and ran == [{"query": "x"}]


def test_for_ear():
    assert for_ear("The meeting is at 17:15 on 2026-11-02.") == \
        "The meeting is at quarter past 5 in the evening on 2 November."
    assert for_ear("Due 3 November 2026, at 9:40 am") == "Due 3 November, at 20 to 10 in the morning"
    assert for_ear("Population 12,345,678 and 18,950, 1,200 and 2026.") == \
        "Population about 12.3 million and about 19 thousand, 1200 and 2026."
    assert for_ear("Battery 85% & 24°C") == "Battery 85 percent and 24 degrees"


def test_misheard_names_are_put_right(tmp_path):
    g = ObsidianVault(str(tmp_path))
    g.write_jarvis_page("jarvis/vocabulary", "- Chiku\n- Rohan")
    audit = AuditStore(":memory:")
    audit.record(request_id="r", source="web", tool="adb_call", risk="destructive", arguments={"to": "Aaditya"},
                 status="ok")
    v = Vocabulary(g, audit)
    assert v.correct("Text Chico that I'm late") == "Text Chiku that I'm late"
    assert v.correct("Call Aditya and Rohann") == "Call Aaditya and Rohan"
    assert v.correct("What's the weather in Pune") == "What's the weather in Pune"


def test_announcer_turns_timers_and_calls_into_satellite_announcements():
    bus = EventBus()
    Announcer(bus, Settings(_env_file=None))
    got = bus.subscribe(Source.SATELLITE)
    bus.publish(Event("timer", Source.SATELLITE, "r", {"label": "pasta"}))
    bus.publish(Event("timer", Source.WEB, "r", {"label": "hud"}))  # the HUD rings its own
    bus.publish(Event("hook_event", Source.WEB, "", {"kind": "call_incoming", "text": "Mum on Pixel 9a"}))
    texts = []
    while not got.empty():
        ev = got.get_nowait()
        if ev.type == "announce":
            texts.append(ev.data["text"])
    assert texts == ["Your pasta timer is done, sir.", "Incoming call: Mum on Pixel 9a."]


def test_a_spoken_yes_in_the_middle_of_several_parts_carries_on_with_the_rest():
    agent, bus, ran = setup([call("web_search", query="Pune weather"), say("Pune is clear, sir."),
                             call("web_search", query="Delhi weather"), say("Delhi is hot, sir.")])
    first = turn(agent, bus, "search the web for the weather in Pune, then search the web for the weather in Delhi")
    assert first == "That will search the web for Pune weather. Shall I go ahead, sir?"
    assert turn(agent, bus, "yes") == "Pune is clear, sir. That will search the web for Delhi weather. Shall I go ahead, sir?"
    assert turn(agent, bus, "yes") == "Pune is clear, sir. Delhi is hot, sir."
    assert ran == [{"query": "Pune weather"}, {"query": "Delhi weather"}]
