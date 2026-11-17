"""Notifications to you: a Windows toast on this laptop, a notification on the phone (over ADB, or the Home
Assistant notify service when no phone is connected), a line and a system notification in the HUD, and
spoken on the satellites. Watchers use it directly; skills and chat use the send_notification tool.

Only ever to you: there is no recipient argument, so a notification can't become a message to someone
else. That's why the tool is a self-scoped write (no approval), and why outside text in it is fine."""

import logging

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Settings, Source
from jarvis_hub.devices.tools import bridge_call
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

log = logging.getLogger(__name__)
TARGETS = ("windows", "android", "hud", "voice")
ALIASES = {"laptop": "windows", "pc": "windows", "computer": "windows", "desktop": "windows", "phone": "android",
           "mobile": "android", "screen": "hud", "speaker": "voice", "satellite": "voice", "say": "voice"}


def targets_from(value) -> list[str]:
    words = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    if any(str(w).lower() == "all" for w in words):
        return list(TARGETS)
    out = [ALIASES.get(str(w).lower(), str(w).lower()) for w in words]
    return [t for t in dict.fromkeys(out) if t in TARGETS]


class Notifier:
    def __init__(self, bus: EventBus, settings: Settings, announcer=None) -> None:
        self.bus, self.s, self.announcer = bus, settings, announcer

    async def send(self, title: str, text: str, targets: list[str], url: str = "", origin: str = "",
                   request_id: str = "") -> dict:
        """Shows it on each target; returns {target: "ok" | error}."""
        title, text = (title or "JARVIS").strip()[:120], (text or "").strip()[:400]
        done: dict[str, str] = {}
        for t in targets_from(targets) or ["hud"]:
            if t == "windows":
                ok, res = await bridge_call(self.s, "win_notify", {"title": title, "text": text, "url": url}, 30)
                done[t] = "ok" if ok else str(res)
            elif t == "android":
                ok, res = await bridge_call(self.s, "adb_notify", {"title": title, "text": text}, 30)
                if not ok and self.s.ha_notify_service and self.announcer:  # no phone on ADB: the HA app push
                    await self.announcer._ha(f"notify/{self.s.ha_notify_service}", {
                        "title": title, "message": text, "data": {"url": url or self.s.hud_url,
                                                                  "clickAction": url or self.s.hud_url}})
                    ok, res = True, "via Home Assistant"
                done[t] = "ok" if ok else str(res)
            elif t == "hud":
                for src in (Source.WEB, Source.PHONE):
                    self.bus.publish(Event("notify", src, request_id, {"title": title, "text": text, "url": url,
                                                                "origin": origin}))
                done[t] = "ok"
            elif t == "voice":
                self.bus.publish(Event("announce", Source.SATELLITE, "", {"text": f"{title}: {text}",
                                                                           "kind": "notify"}))
                done[t] = "ok"
        if any(v != "ok" for v in done.values()):
            log.warning("notification %r: %s", title, done)
        return done


def register_notify_tool(reg: ToolRegistry, notifier: Notifier) -> None:
    async def notify(args: dict, ctx: ToolCtx) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return "ERROR: text is empty. Call send_notification again with the text to show."
        targets = targets_from(args.get("to")) or ["windows", "hud"]
        done = await notifier.send(str(args.get("title") or "JARVIS"), text, targets, origin="jarvis",
                                   request_id=ctx.request_id)
        failed = {t: r for t, r in done.items() if r != "ok"}
        if failed and len(failed) == len(done):
            return ("ERROR: not shown: " + "; ".join(f"{t}: {r}" for t, r in failed.items())
                    + ". Tell the user it couldn't be shown; don't call it again.")
        return "Notified on " + ", ".join(t for t, r in done.items() if r == "ok") + (
            f" (not on {', '.join(failed)}: {'; '.join(failed.values())})" if failed else "") + "."

    reg.add(Tool("notify", "builtin",
                 "Show the user a notification: a pop-up on this laptop, on their phone, in the HUD, or spoken. "
                 "Use for: 'notify me on my phone when…', 'ping my laptop saying stand up', 'pop up a reminder', and in "
                 "skills to alert the user. Only to the user.",
                 {"type": "object", "properties": {
                     "title": {"type": "string"},
                     "text": {"type": "string"},
                     "to": {"type": "string", "description": "where: laptop, phone, hud, voice or all (default laptop and hud)"}},
                  "required": ["text"]},
                 Risk.WRITE, notify, self_scoped=True))
