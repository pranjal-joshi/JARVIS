"""Traces: every turn kept under its conversation, as the model saw it, with personal message text left out."""

import asyncio
import json
import time

from test_harness import ScriptLLM, call, say, setup

from jarvis_hub.approval.gate import Risk
from jarvis_hub.audit.traces import TraceStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.mcp.registry import Tool


def turn(a, text, session=None):
    return asyncio.run(a.run(messages=[{"role": "user", "content": text}], source=Source.WEB,
                             request_id=f"r{time.time_ns()}", bus=EventBus(), trace_session=session))


def test_store_groups_turns_by_session_and_forgets_old_ones(tmp_path):
    store = TraceStore(str(tmp_path / "traces.db"), days=30)
    for i, (session, said) in enumerate([("web-a", "hello"), ("web-a", "time?"), ("web-b", "lock it")]):
        store.record(request_id=f"r{i}", session=session, source="web", route="model", model="m", seconds=1.0,
                     said=said, reply="ok", data={"loops": []})
    listed = {s["session"]: s for s in store.sessions()}
    assert listed["web-a"]["turns"] == 2 and listed["web-a"]["first"] == "hello"
    assert [t["said"] for t in store.session("web-a")] == ["hello", "time?"]
    store._conn.execute("UPDATE turns SET ts = ts - 40 * 86400 WHERE session = 'web-b'")
    assert store.prune() == 1 and [s["session"] for s in store.sessions()] == ["web-a"]
    off = TraceStore(str(tmp_path / "off.db"), days=0)  # TRACE_DAYS=0: nothing is kept
    off.record(request_id="r", session="s", source="web", route="model", model="m", seconds=1, said="x",
               reply="y", data={})
    assert off.sessions() == []


def test_a_turn_is_kept_as_the_model_saw_it():
    a, _ = setup(ScriptLLM([call("send_notification", text="stand up"), say("It is noon, sir.")]))
    a.traces = TraceStore(":memory:")
    turn(a, "ping my laptop saying stand up", session="web-20261103-151204-k3f9")
    [t] = a.traces.session("web-20261103-151204-k3f9")
    assert (t["said"], t["route"], t["source"]) == ("ping my laptop saying stand up", "model", "web")
    loop = t["data"]["loops"][0]
    assert "send_notification" in loop["offered"]
    assert loop["calls"][0]["tool_calls"][0]["name"] == "send_notification"
    assert any(m["role"] == "tool" and "notify ok" in m["content"] for m in loop["convo"])  # the tool result
    assert t["data"]["messages"] == [{"role": "user", "content": "ping my laptop saying stand up"}]
    turn(a, "what time is it")  # no session id: a session of one turn
    assert len(a.traces.sessions()) == 2


def test_personal_message_text_stays_out_of_the_trace():
    a, _ = setup(ScriptLLM([call("adb_sms_read"), say("Mum wrote about dinner, sir.")]))

    async def sms(args, ctx):
        return "Mum: dinner at 8, bring the cake"

    a.tools.add(Tool("adb_sms_read", "android", "read texts", {"type": "object", "properties": {}}, Risk.READ, sms))
    a.traces = TraceStore(":memory:")
    turn(a, "read me my latest texts", session="web-s")
    kept = json.dumps(a.traces.session("web-s")[0]["data"]["loops"])
    assert "bring the cake" not in kept and "not kept in the audit" in kept


def test_the_hud_session_id_reaches_the_agent(client):
    seen = []

    class Capture:
        async def run(self, **kw):
            seen.append(kw.get("trace_session"))
            return "ok"

    client.app.state.agent = Capture()
    web = {"Authorization": "Bearer web-token"}
    body = {"messages": [{"role": "user", "content": "hi"}]}
    client.post("/v1/chat/completions", json=body, headers={**web, "X-Jarvis-Session": "20261103-151204-k3f9"})
    client.post("/v1/chat/completions", json=body, headers={**web, "X-Jarvis-Session": "../../etc"})
    client.post("/v1/chat/completions", json=body,
                headers={"Authorization": "Bearer sat-token", "X-Satellite-Id": "kitchen"})
    assert seen[0] == "web-20261103-151204-k3f9" and seen[1] is None
    assert seen[2].startswith("satellite-kitchen-2")
