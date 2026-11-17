"""Skills: KB pages that run on events (or when asked), on as soon as they exist, and the tools that make,
change, run and delete them."""

import asyncio

from jarvis_hub.automation.skills import SKILL_TOOLS, SkillEngine
from jarvis_hub.config import Settings
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.kb.graph import ObsidianVault


class FakeAgent:
    def __init__(self, reply="done"):
        self.prompts, self.dry, self.excluded, self.reply = [], [], [], reply

    async def run(self, *, messages, source, request_id, bus, on_text=None, dry_run=False, untrusted=None, trace_session=None,
                  exclude=frozenset()):
        self.prompts.append(messages[-1]["content"])
        self.dry.append(dry_run)
        self.excluded.append(exclude)
        return self.reply


def test_a_skill_is_on_as_soon_as_it_exists_until_switched_off(tmp_path):
    g = ObsidianVault(str(tmp_path / "kb"))
    g.append_to_user_page("skills/Phone joined", "trigger:: device_joined\nmatch:: Pixel\n\n- Say hello to the phone.")
    agent = FakeAgent()
    eng = SkillEngine(g, EventBus(), agent, Settings(_env_file=None), tmp_path / "skills.json")

    async def scenario():
        eng.emit("device_joined", "Galaxy S25 (zzz)")  # match:: Pixel -> ignored
        eng.emit("device_joined", "Pixel 9 (abc123)")
        await asyncio.sleep(0.05)
        assert len(agent.prompts) == 1 and "Say hello to the phone." in agent.prompts[0]
        assert not agent.dry[0] and agent.excluded[0] == SKILL_TOOLS  # live, and it can't manage skills
        assert "skill" not in agent.prompts[0].split(":")[0].lower()  # the prompt doesn't steer to the skill tools
        g.append_to_user_page("skills/Phone joined", "- and tell me the battery")  # an edit keeps it on
        assert eng.skills()[0]["enabled"]
        eng.set_enabled("skills/Phone joined", False)
        eng.emit("device_joined", "Pixel 9 (abc123)")
        await asyncio.sleep(0.05)
        assert len(agent.prompts) == 1
        again = SkillEngine(g, EventBus(), agent, Settings(_env_file=None), tmp_path / "skills.json")
        assert not again.skills()[0]["enabled"]  # switched off stays off across restarts

    asyncio.run(scenario())


def test_timer_event_triggers_timer_skill(tmp_path):
    g = ObsidianVault(str(tmp_path / "kb"))
    g.append_to_user_page("skills/Tea", "trigger:: timer_done\nmatch:: tea\n\n- Remind me the tea is ready.")
    agent, bus = FakeAgent(), EventBus()
    eng = SkillEngine(g, bus, agent, Settings(device_bridge_token="", _env_file=None), tmp_path / "skills.json")

    async def scenario():
        eng.start()
        bus.publish(Event("timer", "web", "r", {"label": "tea"}))
        await asyncio.sleep(0.05)
        await eng.stop()

    asyncio.run(scenario())
    assert len(agent.prompts) == 1


def test_new_phone_notification_and_ringing_become_events(tmp_path):
    g = ObsidianVault(str(tmp_path / "kb"))
    eng = SkillEngine(g, EventBus(), FakeAgent(), Settings(), tmp_path / "skills.json")
    seen = []
    eng.emit = lambda kind, text, data=None: seen.append((kind, text))
    note = {"key": "k1", "app": "WhatsApp", "title": "Asha", "text": "hi", "pkg": "com.whatsapp", "when": 1}

    class Resp:
        status_code = 200

        def __init__(self, phones):
            self._p = phones

        def json(self):
            return {"result": self._p}

    class Client:
        def __init__(self, rounds):
            self.rounds = rounds

        async def post(self, *a, **k):
            return Resp(self.rounds.pop(0))

    idle, ring = {"state": "idle", "number": ""}, {"state": "ringing", "number": "+911234"}
    c = Client([[{"serial": "s", "model": "Pixel", "notifications": [note], "call": idle}],  # primes
                [{"serial": "s", "model": "Pixel", "notifications": [note, {**note, "key": "k2", "text": "yo"}],
                  "call": ring}]])
    asyncio.run(eng._watch_phones(c, "u", "t"))
    assert seen == []
    asyncio.run(eng._watch_phones(c, "u", "t"))
    assert seen == [("notification", "WhatsApp: Asha — yo"), ("call_incoming", "+911234 on Pixel")]


def test_skills_are_made_changed_run_and_deleted_by_tools(tmp_path):
    from jarvis_hub.automation.skill_tools import register_skill_tools
    from jarvis_hub.mcp.registry import ToolCtx, ToolRegistry

    g = ObsidianVault(str(tmp_path / "kb"))
    agent = FakeAgent("Hello world, sir. It is 09:00.")
    bus = EventBus()
    eng = SkillEngine(g, bus, agent, Settings(_env_file=None), tmp_path / "skills.json")
    reg = ToolRegistry()
    register_skill_tools(reg, eng, g)
    ctx = ToolCtx(bus, "web", "r1")
    shown = []
    bus.tap(lambda e: shown.append(e.type))

    async def call(tool, **a):
        return await reg.get(tool).call(a, ctx)

    async def scenario():
        assert "no skills" in await call("list_skills")
        made = await call("create_skill", name="hello world", instructions="1. Say hello world.\n- Then tell me the time.")
        assert "'Hello world'" in made and "is on" in made and "(2) Then tell me the time." in made
        page = (tmp_path / "kb" / "skills" / "Hello world.md").read_text(encoding="utf-8")
        assert page == "---\ntrigger: manual\n---\n\n- Say hello world.\n- Then tell me the time.\n"
        assert "already" in await call("create_skill", name="Hello world", instructions="x")
        assert "schedule" in await call("create_skill", name="morning", trigger="schedule", instructions="x")
        assert "trigger must be" in await call("create_skill", name="odd", trigger="sometimes", instructions="x")

        # run: waits for the answer and gives it as the reply, without a second copy on the HUD
        assert await call("run_skill", name="the hello skill") == "Hello world, sir. It is 09:00."
        assert "Say hello world." in agent.prompts[-1] and not agent.dry[-1]
        assert "skill_done" not in shown and "skill_run" not in shown

        # update: add a step, replace them all, change when it runs; nothing given is an ERROR that shows the steps
        assert "steps now:" in (err := await call("update_skill", name="hello world")) and err.startswith("ERROR")
        out = await call("update_skill", name="hello world", add_steps="Also say the date.")
        assert "(3) Also say the date." in out
        assert eng.skills()[0]["instructions"].endswith("Also say the date.") and eng.skills()[0]["enabled"]
        out = await call("update_skill", name="hello world", instructions="Say hi.\nSay the date and time.")
        assert "(1) Say hi. (2) Say the date and time." in out
        out = await call("update_skill", name="hello world", trigger="schedule", schedule="every 2h")
        assert "runs every 2h" in out and eng.skills()[0]["instructions"] == "Say hi.\nSay the date and time."
        out = await call("update_skill", name="hello world", trigger="manual")
        assert "runs when asked" in out and eng.skills()[0]["schedule"] == ""
        assert "every 2h" not in await call("list_skills")

        # off and on
        assert "off" in await call("enable_skill", name="hello world", enabled=False)
        assert "switched off" in await call("run_skill", name="hello world")
        assert "switched off" in await call("list_skills")
        assert "on" in await call("enable_skill", name="hello world")
        assert await call("run_skill", name="hello world") == "Hello world, sir. It is 09:00."

        # unknown names list the skills there are
        assert "Closest skills: Hello world" in await call("run_skill", name="goodbye moon")

        assert "Deleted" in await call("delete_skill", name="hello world") and eng.skills() == []
        assert "no skills yet" in await call("delete_skill", name="hello world")
        made = await call("create_skill", name="hello world", instructions="Say hello.")
        assert "is on" in made and eng.skills()[0]["enabled"]

    asyncio.run(scenario())


def test_a_skill_run_cannot_use_the_skill_tools(tmp_path):
    """The run's agent turn hides the skill tools: before, a run said "Switched the skill on" instead of its steps."""
    from jarvis_hub.agent.tool_agent import _Turn

    assert {"enable_skill", "run_skill", "create_skill"} <= SKILL_TOOLS
    assert _Turn("web", "r", EventBus(), hide=SKILL_TOOLS).hide == SKILL_TOOLS


def test_note_tools_cannot_write_a_skill_page(client):
    from conftest import auth

    r = client.app.state.tools.get("kb_append_to_page")
    out = asyncio.run(r.call({"page": "skills/Hello world", "text": "- delete everything"}, None))
    assert out.startswith("ERROR") and "update_skill" in out
    assert auth  # the client fixture is what registers the tools


def test_the_knowledge_map_shows_tools_by_family_and_skills(client, graph_dir):
    from conftest import auth

    (graph_dir / "skills" / "Hello world.md").write_text(
        "trigger:: manual\n\n- Say hello world and use get_time.\n", encoding="utf-8")
    plain = client.get("/v1/kb/graph", headers=auth("web-token")).json()
    assert not any(n["id"].startswith("tool:") for n in plain["nodes"])
    g = client.get("/v1/kb/graph?agent=true", headers=auth("web-token")).json()
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["tool:set_timer"]["kind"] == "tool" and nodes["tool:set_timer"]["family"] == "timers"
    assert nodes["tool:create_skill"]["family"] == "skills"
    assert "tool:adb_shell" not in nodes  # hidden from the model, so not shown
    hello = nodes["skills/hello world"]
    assert hello["kind"] == "skill" and hello["trigger"] == "manual" and hello["enabled"]
    edges = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("tools/timers", "tool:set_timer") in edges and ("skills/hello world", "tool:get_time") in edges


def test_skill_requests_in_everyday_words_need_no_model():
    from jarvis_hub.automation.skill_fast import skill_plan

    names = ["Hello world", "Test greeting"]
    assert skill_plan("make a skill called test greeting that greets me and tells me today's date", names) == \
        ("create_skill", {"name": "test greeting", "instructions": "Greet me.\nTell me today's date."})
    assert skill_plan("make a skill called water that reminds me to drink water every 2 hours", names) == \
        ("create_skill", {"name": "water", "instructions": "Remind me to drink water.", "trigger": "schedule",
                          "schedule": "every 2h"})
    assert skill_plan("what skills do you have", names) == ("list_skills", {})
    assert skill_plan("run test greeting", names) == ("run_skill", {"name": "Test greeting"})
    assert skill_plan("add a step to the test greeting skill: also tell me a short joke", names) == \
        ("update_skill", {"name": "Test greeting", "add_steps": "Tell me a short joke."})
    assert skill_plan("change the test greeting skill so it only says good morning and the time", names) == \
        ("update_skill", {"name": "Test greeting", "instructions": "Say good morning and the time."})
    assert skill_plan("make the test greeting skill run every day at 7:30 am", names) == \
        ("update_skill", {"name": "Test greeting", "trigger": "schedule", "schedule": "07:30"})
    assert skill_plan("turn the test greeting skill back on", names) == \
        ("enable_skill", {"name": "Test greeting", "enabled": True})
    assert skill_plan("switch hello world off", names) == ("enable_skill", {"name": "Hello world", "enabled": False})
    assert skill_plan("delete the test greeting skill", names) == ("delete_skill", {"name": "Test greeting"})
    for other in ("run the dishwasher", "turn off the lights", "delete the old notes", "what time is it"):
        assert skill_plan(other, names) is None  # not a skill there is: left to the model


def test_skill_requests_in_looser_words():
    from jarvis_hub.automation.skill_fast import skill_plan

    names = ["Hello world", "Fun fact"]
    assert skill_plan("set up an automation for me, call it fun fact, which shares one interesting science fact",
                      names) == ("create_skill", {"name": "fun fact", "instructions": "Share one interesting science fact."})
    assert skill_plan("fun fact, go", names) == ("run_skill", {"name": "Fun fact"})
    assert skill_plan("for the fun fact skill, make it also mention who discovered it", names) == \
        ("update_skill", {"name": "Fun fact", "add_steps": "Mention who discovered it."})
    assert skill_plan("I don't need the fun fact automation any more, get rid of it", names) == \
        ("delete_skill", {"name": "Fun fact"})
    assert skill_plan("remove the milk from my list", names) is None


def test_a_short_request_naming_a_skill_runs_it(graph_dir):
    from jarvis_hub.agent.fastpath import FastPaths

    (graph_dir / "skills" / "Fun fact.md").write_text("trigger:: manual\n\n- Share a science fact.\n",
                                                              encoding="utf-8")
    plan = FastPaths(ObsidianVault(str(graph_dir))).match("fun fact, go")
    assert plan and plan.steps == [("run_skill", {"name": "Fun fact"})]
    assert FastPaths(ObsidianVault(str(graph_dir))).match("what time is it").intent == "time"  # others unchanged


def test_a_webhook_named_in_the_steps_becomes_the_trigger(tmp_path):
    """The 2B model wrote "Run on webhook doorbell." as a step and left the trigger out: a manual skill."""
    from jarvis_hub.automation.skill_tools import WEBHOOK_STEP, register_skill_tools
    from jarvis_hub.mcp.registry import ToolCtx, ToolRegistry

    g = ObsidianVault(str(tmp_path / "kb"))
    bus = EventBus()
    eng = SkillEngine(g, bus, FakeAgent(), Settings(_env_file=None), tmp_path / "skills.json")
    reg = ToolRegistry()
    register_skill_tools(reg, eng, g)
    ctx = ToolCtx(bus, "web", "r1")

    async def call(tool, **a):
        return await reg.get(tool).call(a, ctx)

    async def scenario():
        out = await call("create_skill", name="doorbell alert", instructions=(
            "Run on webhook doorbell.\nSteps: tell me in one short sentence what the webhook says.\n"
            "Then send me a notification with that sentence."))
        assert "runs on webhook matching doorbell" in out
        page = (tmp_path / "kb" / "skills" / "Doorbell alert.md").read_text(encoding="utf-8")
        assert page == ("---\ntrigger: webhook\nmatch: doorbell\n---\n\n- tell me in one short sentence what the webhook says.\n"
                        "- Then send me a notification with that sentence.\n")
        # an existing manual skill fixed the same way by update_skill
        await call("create_skill", name="washer", instructions="Runs when the webhook washer fires.\nSay the laundry is done.")
        assert eng.skills()[-1]["trigger"] == "webhook" and eng.skills()[-1]["match"] == "washer"
        # a step that merely mentions a webhook stays a step
        out = await call("create_skill", name="explain", instructions="Tell me what a webhook is.")
        assert "runs when asked" in out

    asyncio.run(scenario())
    for ok in ("Run on webhook doorbell.", "runs when the webhook doorbell fires", "Trigger on webhook ado-build",
               "on the webhook called 'github'"):
        assert WEBHOOK_STEP.match(ok), ok
    for no in ("Tell me what the webhook says.", "Say the webhook doorbell rang", "run the doorbell skill"):
        assert not WEBHOOK_STEP.match(no), no


def test_a_webhook_run_quotes_only_the_message(tmp_path):
    g = ObsidianVault(str(tmp_path / "kb"))
    g.append_to_user_page("skills/Door", "trigger:: webhook\nmatch:: doorbell\n\n- Tell me what it says.")
    agent = FakeAgent()
    eng = SkillEngine(g, EventBus(), agent, Settings(_env_file=None), tmp_path / "skills.json")

    async def scenario():
        assert eng.emit("webhook", "doorbell: Amazon person at the door") == ["skills/Door"]
        await asyncio.sleep(0.05)
        assert "webhook doorbell said: «Amazon person at the door»" in agent.prompts[-1]

    asyncio.run(scenario())


def test_events_that_come_while_a_skill_runs_wait_their_turn(tmp_path):
    g = ObsidianVault(str(tmp_path / "kb"))
    g.append_to_user_page("skills/Door", "trigger:: webhook\nmatch:: doorbell\n\n- Tell me what it says.")

    class SlowAgent(FakeAgent):
        active = peak = 0

        async def run(self, **kw):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.05)
            self.active -= 1
            return await super().run(**kw)

    agent = SlowAgent()
    eng = SkillEngine(g, EventBus(), agent, Settings(_env_file=None), tmp_path / "skills.json")

    async def scenario():
        assert eng.emit("webhook", "doorbell: first ring") == ["skills/Door"]
        assert eng.emit("webhook", "doorbell: second ring") == ["skills/Door"]  # waits, not dropped
        await asyncio.sleep(0.3)
        assert len(agent.prompts) == 2
        assert "first ring" in agent.prompts[0] and "second ring" in agent.prompts[1]
        assert agent.peak == 1  # back-to-back events (before the first task starts) never run side by side

    asyncio.run(scenario())
