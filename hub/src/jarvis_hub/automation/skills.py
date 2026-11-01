"""Skills: small automations written as knowledge-base pages, run when an event fires.

A skill is a Logseq page under skills/ whose page properties say when it runs, and whose blocks
are the instructions JARVIS follows, e.g. page `skills/Phone joined`:

    trigger:: device_joined
    match:: Pixel
    - Tell me the phone's battery level and open Spotify on it.

Triggers: device_joined, device_left (Android over ADB), timer_done, schedule.
`match::` (optional) must appear in the event text (device model/serial, timer label).
`schedule::` for trigger schedule: a daily time like `07:30`, or an interval like `every 30m` / `every 2h`.

Safety: KB pages are data, so a skill never runs just because it exists. It runs only after the user
enables it in the HUD, and only while the page is unchanged since then (its text hash is pinned; an
edit switches it off until re-enabled). Runs go through the normal agent: every tool is audited and
destructive/external tools still wait for approval.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from jarvis_hub.config import Settings, Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.kb.graph import LogseqGraph, Page

log = logging.getLogger(__name__)
NS = "skills/"
TRIGGERS = {"device_joined", "device_left", "timer_done", "schedule"}
EVERY = re.compile(r"^every\s+(\d+)\s*([mh])$", re.I)
DAILY = re.compile(r"^(\d{1,2}):(\d{2})$")
RUN_SOURCE = Source.WEB  # results show on the HUD; approvals go to the HUD/phone as usual


def _digest(page: Page) -> str:
    return hashlib.sha256(page.path.read_bytes()).hexdigest()


class SkillEngine:
    def __init__(self, graph: LogseqGraph, bus: EventBus, agent, settings: Settings, state_file: Path) -> None:
        self.graph, self.bus, self.agent, self.settings = graph, bus, agent, settings
        self.state_file = state_file
        self.enabled: dict[str, str] = self._load()  # skill name (lower) -> pinned sha256
        self.last_run: dict[str, float] = {}
        self.devices: dict[str, dict] | None = None  # serial -> device, None until first poll
        self.recent: list[dict] = []  # recent events, newest first, for the HUD
        self._tasks: list[asyncio.Task] = []
        self._running: set[str] = set()
        self._watchers: list[asyncio.Task] = []

    # ---------- state ----------
    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_file.write_text(json.dumps(self.enabled, indent=1), encoding="utf-8")

    def skills(self) -> list[dict]:
        out = []
        for p in self.graph.pages():
            if p.is_journal or not p.name.lower().startswith(NS):
                continue
            key = p.name.lower()
            pinned = self.enabled.get(key)
            digest = _digest(p)
            out.append({
                "name": p.name,
                "trigger": p.properties.get("trigger", "").strip().lower(),
                "match": p.properties.get("match", "").strip(),
                "schedule": p.properties.get("schedule", "").strip(),
                "instructions": "\n".join(b.text for b in p.blocks)[:2000],
                "enabled": pinned == digest,
                "changed": bool(pinned) and pinned != digest,  # edited after it was enabled
                "last_run": self.last_run.get(key),
            })
        return out

    def set_enabled(self, name: str, on: bool) -> dict:
        page = self.graph.page(name)
        if page is None or not page.name.lower().startswith(NS):
            raise KeyError(name)
        if on:
            self.enabled[page.name.lower()] = _digest(page)
        else:
            self.enabled.pop(page.name.lower(), None)
        self._save()
        return next(s for s in self.skills() if s["name"].lower() == page.name.lower())

    # ---------- events ----------
    def emit(self, kind: str, text: str, data: dict | None = None) -> None:
        ev = {"kind": kind, "text": text, "data": data or {}, "ts": time.time()}
        self.recent = [ev, *self.recent][:30]
        self.bus.publish(Event("hook_event", RUN_SOURCE, "", ev))
        for s in self.skills():
            if s["enabled"] and s["trigger"] == kind and (not s["match"] or s["match"].lower() in text.lower()):
                self._spawn(s, f"{kind}: {text}")

    def on_bus(self, event: Event) -> None:
        if event.type == "timer":
            self.emit("timer_done", event.data.get("label", "timer"), event.data)

    def _spawn(self, skill: dict, why: str) -> None:
        key = skill["name"].lower()
        if key in self._running:
            return  # one run of a skill at a time
        self.last_run[key] = time.time()
        task = asyncio.get_running_loop().create_task(self.run(skill, why))
        self._tasks.append(task)
        task.add_done_callback(self._tasks.remove)

    async def run(self, skill: dict, why: str) -> str:
        key = skill["name"].lower()
        self._running.add(key)
        rid = uuid.uuid4().hex
        self.bus.publish(Event("skill_run", RUN_SOURCE, rid, {"skill": skill["name"], "why": why}))
        prompt = (f"(Automation) The skill \"{skill['name']}\" was triggered by {why}. "
                  f"Carry out these instructions, which the user wrote and enabled:\n{skill['instructions']}")
        try:
            reply = await self.agent.run(messages=[{"role": "user", "content": prompt}], source=RUN_SOURCE,
                                         request_id=rid, bus=self.bus)
        except Exception as e:  # a failing skill must not take the engine down
            log.exception("skill %s failed", skill["name"])
            reply = f"The skill failed: {e}"
        finally:
            self._running.discard(key)
        self.bus.publish(Event("skill_done", RUN_SOURCE, rid, {"skill": skill["name"], "text": reply}))
        return reply

    # ---------- watchers ----------
    async def _watch_devices(self) -> None:
        token = self.settings.device_bridge_token.get_secret_value()
        if not token:
            return
        url = self.settings.device_bridge_url.rstrip("/") + "/v1/act"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as c:
            while True:
                try:
                    r = await c.post(url, json={"action": "adb_list"}, headers={"Authorization": f"Bearer {token}"})
                    if r.status_code == 200:
                        now = {d.get("serial") or d["id"]: d for d in r.json()["result"] if d["state"] == "device"}
                        if self.devices is not None:
                            for k in now.keys() - self.devices.keys():
                                d = now[k]
                                self.emit("device_joined", f"{d.get('model') or d['id']} ({k})", d)
                            for k in self.devices.keys() - now.keys():
                                d = self.devices[k]
                                self.emit("device_left", f"{d.get('model') or d['id']} ({k})", d)
                        self.devices = now
                except (httpx.HTTPError, KeyError, ValueError):
                    pass  # bridge down: try again next round
                await asyncio.sleep(10)

    async def _watch_schedules(self) -> None:
        while True:
            now = datetime.now()
            for s in self.skills():
                if not s["enabled"] or s["trigger"] != "schedule":
                    continue
                key, last = s["name"].lower(), self.last_run.get(s["name"].lower(), 0)
                if m := EVERY.match(s["schedule"]):
                    period = int(m[1]) * (60 if m[2].lower() == "m" else 3600)
                    if time.time() - last >= max(period, 60):
                        self.last_run[key] = time.time()  # first run waits one period
                        if last:
                            self._spawn(s, f"schedule ({s['schedule']})")
                elif m := DAILY.match(s["schedule"]):
                    due = now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
                    if now >= due and datetime.fromtimestamp(last) < due and (now - due).seconds < 300:
                        self._spawn(s, f"schedule ({s['schedule']})")
            await asyncio.sleep(30)

    def start(self) -> None:
        self.bus.tap(self.on_bus)
        loop = asyncio.get_running_loop()
        self._watchers = [loop.create_task(self._watch_devices()), loop.create_task(self._watch_schedules())]

    async def stop(self) -> None:
        for t in [*self._watchers, *self._tasks]:
            t.cancel()
