"""The wit dial: a humour level set by voice or the HUD, per satellite, for a while; and the form of address."""

import asyncio
import time
from datetime import date

from conftest import auth

from jarvis_hub.agent.fastpath import FastPaths, _clean
from jarvis_hub.agent.style import SentenceGate, address, tidy
from jarvis_hub.agent.tool_agent import ToolAgent
from jarvis_hub.approval.gate import ApprovalGate
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.mcp.registry import ToolCtx, ToolRegistry
from jarvis_hub.persona.jarvis import SYSTEM_PROMPT, system_prompt
from jarvis_hub.persona.wit import Wit, parse, register_wit_tool


def test_the_default_prompt_is_unchanged():
    assert 'dry humour; address the user as "sir"; no exclamation marks.' in SYSTEM_PROMPT
    assert "no jokes" in system_prompt(0) and "witty" in system_prompt(2)
    assert "no form of address" in system_prompt(1, "")
    assert abs(len(system_prompt(2)) - len(SYSTEM_PROMPT)) < 120  # a swap, not a longer prompt


def test_levels_by_number_name_or_step():
    assert parse(0, 1) == 0 and parse("playful", 1) == 2 and parse("Zero.", 2) == 0
    assert parse("up", 1) == 2 and parse("up", 2) == 2 and parse("down", 0) == 0 and parse("less", 2) == 1
    assert parse("loud", 1) is None and parse(5, 1) is None and parse(True, 1) is None


def test_a_room_can_differ_and_a_change_can_run_out(tmp_path):
    w = Wit(tmp_path / "wit.json", default=1)
    w.set(0, "satellite:kids")
    w.set(2, "", minutes=60)
    assert w.level("satellite:kids") == 0 and w.level("satellite:study") == 2 and w.level() == 2
    assert Wit(tmp_path / "wit.json").level("satellite:kids") == 0  # survives a restart
    w.levels[""]["until"] = time.time() - 1
    assert w.level() == 1 and "" not in w.levels  # the hour is up: back to the default
    assert w.state()["satellites"] == {"satellite:kids": {"level": 0}}


def test_spoken_requests_take_the_fast_path():
    f = FastPaths(None, "x")

    def steps(u):
        p = f._wit(_clean(u), date.today())
        return p and p.steps[0][1]
    assert steps("Be more serious") == {"level": "down", "here_only": False}
    assert steps("Jarvis, dial the humour up") == {"level": "up", "here_only": False}
    assert steps("wit to zero for an hour") == {"level": "zero", "here_only": False, "minutes": 60}
    assert steps("no jokes in here") == {"level": "0", "here_only": True}
    assert steps("be yourself again")["level"] == "1"
    assert steps("tell me a joke") is None and steps("what is wit") is None


def test_set_wit_by_voice_only_in_this_room(tmp_path):
    reg, w = ToolRegistry(), Wit(tmp_path / "wit.json")
    register_wit_tool(reg, w)
    tool = reg.get("set_wit")
    assert tool.self_scoped and tool.final_answer  # its own manner: never an approval

    def call(args, session=""):
        return asyncio.run(tool.call(args, ToolCtx(EventBus(), "satellite", "r", session=session)))
    assert call({"level": "0", "here_only": True}, "satellite:kids") == "Plain answers from now on in here, sir."
    assert w.level("satellite:kids") == 0 and w.level() == 1
    assert call({"level": "up", "minutes": 60}).startswith("I'll allow myself a little more wit for the next hour")
    assert call({"level": "loud"}).startswith("ERROR")


def test_playful_gets_one_more_sentence_and_the_address_is_swapped():
    four = "One. Two. Three. Four."
    assert tidy(four, Source.SATELLITE) == "One. Two."
    assert tidy(four, Source.SATELLITE, wit=2) == "One. Two. Three."
    assert address("Done, sir: lights off. Sir, a moment.", "ma'am") == "Done, ma'am: lights off. Ma'am, a moment."
    assert address("My apologies, sir, it failed.", "") == "My apologies, it failed."
    assert address("I've read Sir Walter Scott, sir.", "Sam") == "I've read Sir Walter Scott, Sam."
    out = []
    g = SentenceGate(out.append, Source.SATELLITE, wit=0, word="Sam")
    g.feed("Very good, sir. Lights off. ")
    g.flush()
    assert "".join(out) == "Very good, Sam. Lights off."


def test_the_agent_uses_the_rooms_level_and_address(tmp_path):
    seen = []

    class LLM:
        model = "jarvis-2b-gpu"

        async def chat(self, messages, tools=None):
            seen.append(messages[0]["content"])
            return {"role": "assistant", "content": "Quite so, sir."}
    a = ToolAgent(LLM(), ToolRegistry(), ApprovalGate(EventBus(), 1), AuditStore(":memory:"), ask_for=frozenset())
    a.wit, a.address = Wit(tmp_path / "wit.json"), "Sam"
    a.wit.set(0, "satellite:kids")
    reply = asyncio.run(a.run(messages=[{"role": "user", "content": "what is a lemur"}], source=Source.SATELLITE,
                              request_id="r", bus=EventBus(), session="satellite:kids"))
    assert reply == "Quite so, Sam." and "no jokes" in seen[0] and '"Sam"' in seen[0]


def test_the_hud_reads_and_sets_the_dial(client):
    r = client.post("/v1/wit", headers=auth("web-token"), json={"level": "playful", "satellite": "satellite:study"})
    assert r.status_code == 200, r.text
    assert r.json()["satellites"]["satellite:study"]["level"] == 2
    assert client.post("/v1/wit", headers=auth("web-token"), json={"level": "loud"}).status_code == 400
    assert client.post("/v1/wit", headers=auth("sat-token"), json={"level": 0}).status_code == 403
    r = client.delete("/v1/wit", headers=auth("web-token"), params={"satellite": "satellite:study"})
    assert r.json()["satellites"] == {}
