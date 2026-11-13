"""Watchers: look-only checks you define (new PRs ready to merge, a pipeline that finished, new mail...),
run on the host by the device bridge on a schedule. Each run's items are compared with what was seen
before; new ones become notifications (on the targets the watcher names) and a `watch` event that
skills can trigger on (`trigger:: watch`, `match:: <watcher name>`).

The commands live in the bridge's config/watchers.json, written only through the HUD (the model has no
tool for it), and the bridge refuses commands its PowerShell check blocks. The first run of a watcher only
learns what is there, so you aren't flooded with everything that already existed. Item text comes from
outside (PR titles, mail subjects): it is shown to you, and a skill sees it marked as data."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from jarvis_hub.config import Settings, Source
from jarvis_hub.devices.tools import bridge_call
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.files import write_atomic

log = logging.getLogger(__name__)
EVERY = re.compile(r"^(\d+)([mh])$")
MAX_SEEN = 500
MAX_ALERTS = 3  # per run; the rest are summed up in one more line


def period(every: str) -> int:
    m = EVERY.match(every or "")
    return int(m[1]) * (60 if m[2] == "m" else 3600) if m else 300


class WatchEngine:
    def __init__(self, bus: EventBus, settings: Settings, notifier, skills, state_file: Path) -> None:
        self.bus, self.s, self.notifier, self.skills = bus, settings, notifier, skills
        self.state_file = state_file
        try:
            self.state: dict[str, dict] = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.watchers: list[dict] = []
        self.error = ""  # the bridge itself unreachable
        self._listed = 0.0
        self._running: set[str] = set()
        self._task: asyncio.Task | None = None

    def _save(self) -> None:
        try:
            write_atomic(self.state_file, json.dumps(self.state, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("watcher state not saved: %s", e)

    def _event(self, kind: str, name: str, **data) -> None:
        """For the HUD's watch panel: created, saved, deleted, toggled, ran, fired, failed."""
        for src in (Source.WEB, Source.PHONE):
            self.bus.publish(Event("watch", src, "", {"kind": kind, "name": name, **data}))

    # ---------- the list ----------
    async def refresh(self, force: bool = False) -> list[dict]:
        if force or time.time() - self._listed > 60:
            ok, res = await bridge_call(self.s, "watch_list", {}, 15)
            if ok:
                self.watchers, self.error, self._listed = list(res or []), "", time.time()
                names = {w["name"] for w in self.watchers}
                if gone := [n for n in self.state if n not in names]:
                    for n in gone:
                        self.state.pop(n)
                    self._save()
            else:
                self.error = str(res)
        return self.watchers

    def describe(self) -> dict:
        now = time.time()
        out = []
        for w in self.watchers:
            st = self.state.get(w["name"], {})
            last = st.get("last_run", 0)
            out.append({**w, "last_run": last, "next_run": (last + period(w["every"])) if w.get("enabled") else None,
                        "last_error": st.get("last_error", ""), "last_new": st.get("last_new", 0),
                        "last_fired": st.get("last_fired", 0), "recent": st.get("recent", []),
                        "seen": len(st.get("seen", [])), "running": w["name"] in self._running, "now": now})
        return {"watchers": out, "error": self.error}

    # ---------- running ----------
    async def run(self, w: dict, manual: bool = False) -> dict:
        name = w["name"]
        if name in self._running:
            return {"error": "already running"}
        self._running.add(name)
        self._event("running", name)
        st = self.state.setdefault(name, {})
        try:
            ok, res = await bridge_call(self.s, "watch_run", {"name": name}, 120)
        finally:
            self._running.discard(name)
        st["last_run"] = time.time()
        if not ok:
            st["last_error"] = str(res)[:300]
            self._save()
            self._event("failed", name, error=st["last_error"])
            return {"error": st["last_error"]}
        items = res.get("items", [])
        st["last_error"] = ""
        first = "seen" not in st
        seen = set(st.get("seen", []))
        new = [i for i in items if i["key"] not in seen]
        st["seen"] = ([i["key"] for i in items] + [k for k in st.get("seen", []) if k not in {i["key"] for i in items}])[:MAX_SEEN]
        st["last_new"] = 0 if first else len(new)
        if first:
            self._save()
            self._event("learned", name, count=len(items))
            return {"items": items, "new": [], "learned": True}
        if new:
            st["last_fired"] = time.time()
            st["recent"] = ([{"text": i["text"], "url": i["url"], "ts": time.time()} for i in new]
                            + st.get("recent", []))[:10]
        self._save()
        if new:
            await self._fire(w, new)
        self._event("ran", name, new=len(new), manual=manual)
        return {"items": items, "new": new}

    async def _fire(self, w: dict, new: list[dict]) -> None:
        self._event("fired", w["name"], items=[i["text"] for i in new[:MAX_ALERTS]], count=len(new))
        title = w.get("title") or w["name"]
        for item in new[:MAX_ALERTS]:
            if w.get("notify"):
                await self.notifier.send(title, item["text"], w["notify"], url=item.get("url", ""), origin=w["name"])
            if self.skills is not None:
                self.skills.emit("watch", f"{w['name']}: {item['text']}",
                                 {"watcher": w["name"], "text": item["text"], "url": item.get("url", "")})
        if len(new) > MAX_ALERTS and w.get("notify"):
            await self.notifier.send(title, f"and {len(new) - MAX_ALERTS} more", w["notify"], origin=w["name"])

    async def _loop(self) -> None:
        await asyncio.sleep(10)  # let the bridge and the rest of the hub come up
        while True:
            try:
                for w in await self.refresh():
                    last = self.state.get(w["name"], {}).get("last_run", 0)
                    if w.get("enabled") and time.time() - last >= period(w["every"]) and w["name"] not in self._running:
                        await self.run(w)
            except Exception:  # a broken watcher must not stop the others
                log.exception("watcher loop")
            await asyncio.sleep(20)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------- changes from the HUD ----------
    async def save(self, watcher: dict, accept_unverified: bool, replaces: str = "",
                   kind: str = "saved") -> tuple[bool, object]:
        ok, res = await bridge_call(self.s, "watch_save", {"watcher": watcher, "accept_unverified": accept_unverified,
                                                           "replaces": replaces}, 60)
        if ok:
            if replaces and replaces != res["name"]:
                self.state.pop(replaces, None)
            old = next((w for w in self.watchers if w["name"] == (replaces or res["name"])), None)
            if not old or old.get("command") != res["command"] or old.get("key") != res["key"]:
                self.state.get(res["name"], {}).pop("seen", None)  # a changed check learns afresh, no flood
            self._save()
            await self.refresh(force=True)
            self._event(kind if replaces else "created", res["name"])
        return ok, res

    async def delete(self, name: str) -> tuple[bool, object]:
        ok, res = await bridge_call(self.s, "watch_delete", {"name": name}, 30)
        if ok:
            self.state.pop(name, None)
            self._save()
            await self.refresh(force=True)
            self._event("deleted", name)
        return ok, res

    async def set_enabled(self, name: str, on: bool) -> tuple[bool, object]:
        w = next((w for w in await self.refresh(force=True) if w["name"] == name), None)
        if not w:
            return False, "no watcher by that name"
        # it was saved (and any warning accepted) before; only the switch changes
        return await self.save({**w, "enabled": on}, accept_unverified=True, replaces=name,
                               kind="resumed" if on else "paused")
