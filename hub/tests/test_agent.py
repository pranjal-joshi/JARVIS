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
                      reg, gate, audit)

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
                      reg, gate, audit)
    asyncio.run(run(agent, bus))
    assert calls == []
    assert audit.recent()[0]["status"] == "timeout"


def test_unknown_tool_and_tool_results_are_fenced(parts):
    bus, gate, audit, reg, _ = parts
    llm = FakeLLM([call("rm_rf"), call("ha__GetLiveContext"), say("ok")])
    asyncio.run(run(ToolAgent(llm, reg, gate, audit), bus))
    last = llm.seen[-1]
    assert "no such tool" in last[-3]["content"]
    assert last[-1]["content"].startswith("<tool_result")


def test_classify_allow_list():
    allow = {"Get*": "read", "HassTurnOn": "write"}
    assert classify("GetLiveContext", allow, None) is Risk.READ
    assert classify("HassTurnOn", allow, None) is Risk.WRITE
    assert classify("HassUnlock", allow, None) is None          # not listed -> hidden
    assert classify("GetLiveContext", allow, Risk.DESTRUCTIVE) is Risk.DESTRUCTIVE  # hint raises
    assert classify("HassTurnOn", allow, Risk.READ) is Risk.WRITE  # hint never lowers
