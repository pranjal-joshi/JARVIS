"""Things worth saying out loud when nobody asked: a timer finished, a skill reported back, a call
is coming in, a background answer is ready. Each becomes an `announce` event for the satellites
(the emulator speaks it; for a real satellite, Home Assistant's assist_satellite.announce is called
when HA_ANNOUNCE_ENTITY is set). Approval requests can also be pushed to the phone through a Home
Assistant notify service (HA_NOTIFY_SERVICE), so a voice-only user knows where to approve."""

import asyncio
import logging

import httpx

from jarvis_hub.config import Settings, Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.voice.speakable import for_ear, to_speech

log = logging.getLogger(__name__)


class Announcer:
    def __init__(self, bus: EventBus, settings: Settings) -> None:
        self.bus, self.s = bus, settings
        self.kinds = {k.strip() for k in settings.voice_announce.split(",") if k.strip()}
        self._tasks: set[asyncio.Task] = set()
        bus.tap(self.on_event)

    def on_event(self, ev: Event) -> None:
        if ev.type == "announce":
            if self.s.ha_announce_entity:
                self._spawn(self._ha("assist_satellite/announce", {
                    "entity_id": self.s.ha_announce_entity, "message": for_ear(to_speech(ev.data["text"]))}))
            return
        if ev.type == "approval_request" and ev.source is Source.PHONE and self.s.ha_notify_service:
            d = ev.data
            self._spawn(self._ha(f"notify/{self.s.ha_notify_service}", {
                "title": "JARVIS needs your approval",
                "message": f"{d['tool']} ({d['risk']}) asked by {d['requested_by']}",
                "data": {"url": self.s.hud_url, "clickAction": self.s.hud_url, "tag": d["approval_id"],
                         "priority": "high", "ttl": 0}}))
            return
        text, kind = None, None
        if ev.type == "timer" and "timer" in self.kinds and ev.source is Source.SATELLITE:
            text, kind = f"Your {ev.data.get('label') or 'timer'} timer is done, sir.", "timer"
        elif ev.type == "skill_done" and "skill" in self.kinds:
            text, kind = to_speech(ev.data.get("text") or "", 300), "skill"
        elif ev.type == "hook_event" and "call" in self.kinds and ev.data.get("kind") == "call_incoming":
            text, kind = f"Incoming call: {ev.data.get('text')}.", "call"
        if text:
            self.bus.publish(Event("announce", Source.SATELLITE, ev.request_id, {"text": text, "kind": kind}))

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (tests publishing from sync code)
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _ha(self, service: str, body: dict) -> None:
        token = self.s.ha_token.get_secret_value()
        if not token or not self.s.ha_base_url():
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{self.s.ha_base_url()}/api/services/{service}", json=body,
                                 headers={"Authorization": f"Bearer {token}"})
            if r.status_code >= 400:
                log.warning("Home Assistant %s: HTTP %s %s", service, r.status_code, r.text[:200])
        except httpx.HTTPError as e:
            log.warning("Home Assistant %s unreachable: %s", service, e)
