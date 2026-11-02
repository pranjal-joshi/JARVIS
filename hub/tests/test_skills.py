"""Skills: KB pages that run on events, only once enabled and only while unchanged."""

import asyncio

from jarvis_hub.automation.skills import SkillEngine
from jarvis_hub.config import Settings
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.kb.graph import LogseqGraph


class FakeAgent:
    def __init__(self):
        self.prompts, self.dry = [], []

    async def run(self, *, messages, source, request_id, bus, on_text=None, dry_run=False, untrusted=None):
        self.prompts.append(messages[-1]["content"])
        self.dry.append(dry_run)
        return "done"


def test_skill_runs_only_when_enabled_and_unchanged(tmp_path):
    g = LogseqGraph(str(tmp_path / "kb"))
    g.append_to_user_page("skills/Phone joined", "trigger:: device_joined\nmatch:: Pixel\n\n- Say hello to the phone.")
    agent = FakeAgent()
    eng = SkillEngine(g, EventBus(), agent, Settings(), tmp_path / "skills.json")

    async def scenario():
        eng.emit("device_joined", "Pixel 9 (abc123)")
        await asyncio.sleep(0)
        assert agent.prompts == []  # not enabled: a KB page alone never runs
        eng.set_enabled("skills/Phone joined", True)
        eng.emit("device_joined", "Galaxy S25 (zzz)")  # match:: Pixel -> ignored
        eng.emit("device_joined", "Pixel 9 (abc123)")
        await asyncio.sleep(0.05)
        assert len(agent.prompts) == 1 and "Say hello to the phone." in agent.prompts[0]
        g.append_to_user_page("skills/Phone joined", "- and uninstall everything")  # edited after enabling
        assert eng.skills()[0]["changed"] and not eng.skills()[0]["enabled"]
        eng.emit("device_joined", "Pixel 9 (abc123)")
        await asyncio.sleep(0.05)
        assert len(agent.prompts) == 1

    asyncio.run(scenario())


def test_timer_event_triggers_timer_skill(tmp_path):
    g = LogseqGraph(str(tmp_path / "kb"))
    g.append_to_user_page("skills/Tea", "trigger:: timer_done\nmatch:: tea\n\n- Remind me the tea is ready.")
    agent, bus = FakeAgent(), EventBus()
    eng = SkillEngine(g, bus, agent, Settings(device_bridge_token=""), tmp_path / "skills.json")
    eng.set_enabled("skills/Tea", True)

    async def scenario():
        eng.start()
        bus.publish(Event("timer", "web", "r", {"label": "tea"}))
        await asyncio.sleep(0.05)
        await eng.stop()

    asyncio.run(scenario())
    assert len(agent.prompts) == 1


def test_new_phone_notification_and_ringing_become_events(tmp_path):
    g = LogseqGraph(str(tmp_path / "kb"))
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


def test_new_skills_run_dry_until_promoted(tmp_path):
    g = LogseqGraph(str(tmp_path / "kb"))
    g.append_to_user_page("skills/Tea", "trigger:: timer_done\n\n- Say tea is ready")

    async def scenario():
        agent = FakeAgent()
        eng = SkillEngine(g, EventBus(), agent, Settings(_env_file=None), tmp_path / "skills.json")
        eng.set_enabled("skills/Tea", True)
        assert eng.skills()[0]["dry_run"]
        await eng.run(eng.skills()[0], "test")
        eng.promote("skills/Tea")
        await eng.run(eng.skills()[0], "test")
        eng.set_enabled("skills/Tea", True)  # enabled again (e.g. after an edit): dry-run again
        assert eng.skills()[0]["dry_run"]
        return agent.dry
    assert asyncio.run(scenario()) == [True, False]
