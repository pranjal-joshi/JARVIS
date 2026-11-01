"""Skills: KB pages that run on events, only once enabled and only while unchanged."""

import asyncio

from jarvis_hub.automation.skills import SkillEngine
from jarvis_hub.config import Settings
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.kb.graph import LogseqGraph


class FakeAgent:
    def __init__(self):
        self.prompts = []

    async def run(self, *, messages, source, request_id, bus, on_text=None):
        self.prompts.append(messages[-1]["content"])
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
