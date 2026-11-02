"""Agent loop, approval gate and audit, with a scripted fake LLM."""

import asyncio
import json

import pytest

from jarvis_hub.agent.tool_agent import ToolAgent
from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.mcp.registry import Tool, ToolRegistry, classify


ALL_GATED = frozenset({Risk.WRITE, Risk.EXTERNAL, Risk.DESTRUCTIVE})


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def chat(self, messages, tools=None):
        self.seen.append(messages)
        return self.script.pop(0)


def call(tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": tool, "arguments": json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


@pytest.fixture
def parts(tmp_path):
    bus = EventBus()
    gate = ApprovalGate(bus, timeout_s=2)
    audit = AuditStore(str(tmp_path / "a.db"))
    reg = ToolRegistry()
    calls = []

    async def light(args, ctx):
        calls.append(args)
        return "light on"

    async def state(args, ctx):
        return "desk lamp is off"

    reg.add(Tool("ha__HassTurnOn", "ha", "turn on", {}, Risk.WRITE, light))
    reg.add(Tool("ha__GetLiveContext", "ha", "state", {}, Risk.READ, state))
    return bus, gate, audit, reg, calls


async def run(agent, bus, source=Source.WEB, text="hi"):
    return await agent.run(messages=[{"role": "user", "content": text}], source=source,
                           request_id="r1", bus=bus)


def test_read_tool_runs_without_approval(parts):
    bus, gate, audit, reg, _ = parts
    agent = ToolAgent(FakeLLM([call("ha__GetLiveContext"), say("It is off, sir.")]), reg, gate, audit)
    assert asyncio.run(run(agent, bus)) == "It is off, sir."
    row = audit.recent()[0]
    assert (row["tool"], row["status"], row["approved_by"]) == ("ha__GetLiveContext", "ok", "policy")


def test_write_tool_waits_for_web_approval(parts):
    bus, gate, audit, reg, calls = parts
    agent = ToolAgent(FakeLLM([call("ha__HassTurnOn", name="desk lamp"), say("Done, sir.")]),
                      reg, gate, audit, ask_for=ALL_GATED)

    async def scenario():
        approvals = bus.subscribe(Source.WEB)
        task = asyncio.create_task(run(agent, bus, Source.SATELLITE, "desk lamp on"))
        while True:
            ev = await asyncio.wait_for(approvals.get(), 2)
            if ev.type == "approval_request":
                break
        with pytest.raises(PermissionError):
            gate.decide(ev.data["approval_id"], True, Source.SATELLITE)  # voice can't approve
        assert gate.decide(ev.data["approval_id"], True, Source.WEB)
        return await task

    assert asyncio.run(scenario()) == "Done, sir."
    assert calls == [{"name": "desk lamp"}]
    row = audit.recent()[0]
    assert (row["status"], row["approved_by"], row["source"]) == ("ok", "web", "satellite")


def test_unapproved_write_is_not_executed(parts):
    bus, gate, audit, reg, calls = parts
    gate._timeout = 0.05
    agent = ToolAgent(FakeLLM([call("ha__HassTurnOn", name="all"), say("Very well, sir.")]),
                      reg, gate, audit, ask_for=ALL_GATED)
    asyncio.run(run(agent, bus))
    assert calls == []
    assert audit.recent()[0]["status"] == "timeout"


def test_unknown_tool_and_tool_results_are_fenced(parts):
    bus, gate, audit, reg, _ = parts
    llm = FakeLLM([call("rm_rf"), call("ha__GetLiveContext"), say("ok")])
    asyncio.run(run(ToolAgent(llm, reg, gate, audit), bus))
    last = llm.seen[-1]
    assert "no tool called 'rm_rf'" in last[-3]["content"]
    assert last[-1]["content"].startswith("<tool_result")


def test_classify_allow_list():
    allow = {"Get*": "read", "HassTurnOn": "write"}
    assert classify("GetLiveContext", allow, None) is Risk.READ
    assert classify("HassTurnOn", allow, None) is Risk.WRITE
    assert classify("HassUnlock", allow, None) is None          # not listed -> hidden
    assert classify("GetLiveContext", allow, Risk.DESTRUCTIVE) is Risk.DESTRUCTIVE  # hint raises
    assert classify("HassTurnOn", allow, Risk.READ) is Risk.WRITE  # hint never lowers


def test_faked_tool_result_is_retried_then_refused(parts):
    bus, gate, audit, reg, calls = parts
    fake = say('<tool_result>\n{"status": "success"}\n</tool_result>')
    # A fake followed by a real call: the nudge makes the model use the tool.
    llm = FakeLLM([fake, call("ha__GetLiveContext"), say("The lamp is off, sir.")])
    assert asyncio.run(run(ToolAgent(llm, reg, gate, audit), bus)) == "The lamp is off, sir."
    assert any("no tool ran" in m["content"] for m in llm.seen[-1] if m["role"] == "user")
    # Two fakes in a row: never claim success.
    llm = FakeLLM([fake, fake])
    reply = asyncio.run(run(ToolAgent(llm, reg, gate, audit), bus))
    assert "Nothing was changed" in reply and calls == []


def test_final_answer_tool_output_is_the_reply(parts):
    bus, gate, audit, reg, _ = parts

    async def expert(args, ctx):
        return "Forty-two.\n\n— claude · m (1 s)"

    reg.add(Tool("ask_claude", "expert", "x", {}, Risk.READ, expert, final_answer=True))
    llm = FakeLLM([call("ask_claude", question="q"), say("should not be used")])
    reply = asyncio.run(run(ToolAgent(llm, reg, gate, audit), bus))
    assert reply.startswith("Forty-two.") and len(llm.seen) == 1


def test_write_runs_without_approval_by_default(parts):
    bus, gate, audit, reg, calls = parts
    agent = ToolAgent(FakeLLM([call("ha__HassTurnOn", name="desk lamp"), say("Done, sir.")]), reg, gate, audit)
    assert asyncio.run(run(agent, bus)) == "Done, sir."
    assert calls and audit.recent()[0]["approved_by"] == "policy"


def test_stream_filter_drops_think_and_tool_markup():
    from jarvis_hub.agent.tool_agent import _speakable_stream
    out = []
    feed = _speakable_stream(out.append)
    for full in ["<think>hm", "<think>hm</think> Good", "<think>hm</think> Good evening, <",
                 "<think>hm</think> Good evening, <b>sir"]:
        feed(full)
    assert "".join(out) == "Good evening, <b>sir"
    out.clear()
    feed = _speakable_stream(out.append)
    feed("Turning it on")
    feed("Turning it on <tool_call>{...}")
    feed("Turning it on <tool_call>{...} more")
    assert "".join(out) == "Turning it on"


def test_streamed_text_is_reset_when_a_tool_follows(parts):
    bus, gate, audit, reg, _ = parts

    class StreamLLM(FakeLLM):
        async def chat(self, messages, tools=None, on_text=None):
            msg = self.script.pop(0)
            if on_text and msg.get("content"):
                on_text(msg["content"])
            return msg

    first = call("ha__GetLiveContext")
    first["content"] = "Let me check."
    got = []
    agent = ToolAgent(StreamLLM([first, say("It is off, sir.")]), reg, gate, audit)
    reply = asyncio.run(agent.run(messages=[{"role": "user", "content": "lamp?"}], source=Source.WEB,
                                  request_id="r1", bus=bus, on_text=got.append))
    # "Let me check." never ended in a full sentence boundary before the tool call, so nothing was sent
    assert got == ["It is off, sir."] and reply == "It is off, sir."


def test_claimed_save_without_a_tool_is_sent_back_then_refused(parts):
    bus, gate, audit, reg, calls = parts
    # claims "noted" after only a read: nudged once, then it really calls the tool
    llm = FakeLLM([call("ha__GetLiveContext"), say("Noted, sir. I've saved that."), call("ha__HassTurnOn", name="x"),
                   say("Saved, sir.")])
    assert asyncio.run(run(ToolAgent(llm, reg, gate, audit, ask_for=frozenset()), bus)) == "Saved, sir."
    assert calls == [{"name": "x"}]
    assert any("no tool that changes anything ran" in m["content"] for m in llm.seen[-1] if m["role"] == "user")
    # claims it twice: the claim never reaches the user
    llm = FakeLLM([say("I'll remember that, sir."), say("Noted.")])
    assert asyncio.run(run(ToolAgent(llm, reg, gate, audit), bus)).startswith("I haven't actually saved")


def test_answers_that_only_mention_notes_are_not_claims(parts):
    bus, gate, audit, reg, _ = parts
    for text in ["You take it black, as noted on your preferences page.", "The lamp is on, sir.",
                 "It was added to the calendar last week, by the look of it."]:
        assert asyncio.run(run(ToolAgent(FakeLLM([say(text)]), reg, gate, audit), bus)) == text


def test_stated_facts_get_a_remember_hint(parts):
    bus, gate, audit, reg, _ = parts
    for text, hinted in [("my sister Chiku's birthday is 9 June", True),
                         ("I prefer window seats on flights, keep that in mind", True),
                         ("what is my wifi password?", False), ("turn on the lamp", False)]:
        llm = FakeLLM([say("Very good, sir.")])
        asyncio.run(run(ToolAgent(llm, reg, gate, audit), bus, text=text))
        assert ("call remember" in llm.seen[0][-1]["content"]) is hinted, text  # rides on the user turn
        assert "call remember" not in llm.seen[0][0]["content"]  # the system prompt never changes
