import asyncio
import json
import time

from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.mcp.registry import ToolCtx
from jarvis_hub.tools.utility import Timers


def test_timers_are_saved_and_rearmed_after_a_restart(tmp_path):
    path = tmp_path / "timers.json"

    async def first_run():
        timers = Timers(path)
        timers.start(3600, "pasta", ToolCtx(EventBus(), Source.WEB, "r1"))
        timers.start(3600, "tea", ToolCtx(EventBus(), Source.SATELLITE, "r2"))
        timers.cancel("tea")
        for t in timers.active.values():
            t["task"].cancel()

    asyncio.run(first_run())
    saved = json.loads(path.read_text())
    assert [t["label"] for t in saved] == ["pasta"] and saved[0]["source"] == "web"

    async def after_restart():
        bus = EventBus()
        q = bus.subscribe(Source.WEB)
        timers = Timers(path)
        saved[0]["due"] = time.time() - 60  # came due while the hub was down
        path.write_text(json.dumps(saved))
        assert timers.restore(bus) == 1
        ev = await asyncio.wait_for(q.get(), 2)
        return ev, timers

    ev, timers = asyncio.run(after_restart())
    assert ev.type == "timer" and ev.data["label"] == "pasta" and ev.data["late"] is True
    assert not timers.active and json.loads(path.read_text()) == []
