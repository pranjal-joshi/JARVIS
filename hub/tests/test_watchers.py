import asyncio

from jarvis_hub.automation import watchers as wmod
from jarvis_hub.automation.notify import targets_from
from jarvis_hub.automation.watchers import WatchEngine
from jarvis_hub.config import Settings, Source
from jarvis_hub.events.bus import EventBus


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, title, text, targets, url="", origin=""):
        self.sent.append((title, text, tuple(targets)))
        return {t: "ok" for t in targets}


class FakeSkills:
    def __init__(self):
        self.events = []

    def emit(self, kind, text, data=None):
        self.events.append((kind, text))


def test_a_watcher_learns_first_then_notifies_only_new_items(tmp_path, monkeypatch):
    runs = [[{"key": "a", "text": "PR 1", "url": ""}],
            [{"key": "a", "text": "PR 1", "url": ""}, {"key": "b", "text": "PR 2", "url": "https://x/2"}],
            [{"key": "b", "text": "PR 2", "url": ""}]]

    async def fake_bridge(s, action, args, timeout=200):
        return True, {"items": runs.pop(0)}

    monkeypatch.setattr(wmod, "bridge_call", fake_bridge)
    notifier, skills = FakeNotifier(), FakeSkills()
    w = {"name": "prs", "every": "5m", "title": "Ready to merge", "notify": ["windows", "hud"]}

    async def go():
        bus = EventBus()
        q = bus.subscribe(Source.WEB)
        eng = WatchEngine(bus, Settings(), notifier, skills, tmp_path / "state.json")
        first = await eng.run(w)
        second = await eng.run(w)
        third = await eng.run(w)
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait().data["kind"])
        return first, second, third, kinds

    first, second, third, kinds = asyncio.run(go())
    assert first["learned"] and not notifier.sent[:0]
    assert [i["text"] for i in second["new"]] == ["PR 2"]
    assert notifier.sent == [("Ready to merge", "PR 2", ("windows", "hud"))]
    assert skills.events == [("watch", "prs: PR 2")]
    assert third["new"] == []
    assert "fired" in kinds and "learned" in kinds


def test_notification_targets_accept_plain_words():
    assert targets_from("laptop, phone") == ["windows", "android"]
    assert targets_from("all") == ["windows", "android", "hud", "voice"]
    assert targets_from(["hud", "nonsense"]) == ["hud"]
