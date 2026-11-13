"""Reminders: at a time and day, once or repeating (every day, weekdays, one day a week), shown on the phone and
in the HUD (or wherever the user says). Timers only count down up to a day; a 2B model asked for "every morning
at 10 on my phone and HUD" sent a one-off notification instead.

Saved to a small JSON file and re-armed at start-up. A reminder that came due while the hub was down is shown
late (a repeating one only if it is less than CATCH_UP late). Only ever to the user, like a notification, so the
tool needs no approval."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from jarvis_hub.approval.gate import Risk
from jarvis_hub.automation.notify import Notifier, targets_from
from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.guard.validate import best_name, resolver
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry
from jarvis_hub.tools.when import DAYS, spoken, when
from jarvis_hub.files import write_atomic

log = logging.getLogger(__name__)
REPEATS = ("once", "daily", "weekdays", "weekly")
DEFAULT_TO = ["android", "hud"]
CATCH_UP = 6 * 3600
CHECK_S = 20


def next_time(r: dict, after: datetime) -> datetime | None:
    """When a reminder next goes off after `after`: its time on the next day its repeat allows."""
    hour, minute = map(int, r["time"].split(":"))
    if r["repeat"] == "once":
        return None
    d = after.date()
    for _ in range(8):
        at = datetime.combine(d, datetime.min.time()).replace(hour=hour, minute=minute)
        ok = r["repeat"] == "daily" or (r["repeat"] == "weekdays" and d.weekday() < 5) \
            or (r["repeat"] == "weekly" and d.weekday() == r.get("weekday", 0))
        if ok and at > after:
            return at
        d += timedelta(days=1)
    return None


def describe(r: dict) -> str:
    """"every day at 10:00", "on weekdays at 09:00", "every Monday at 18:30", "tomorrow at 10:00"."""
    if r["repeat"] == "daily":
        return f"every day at {r['time']}"
    if r["repeat"] == "weekdays":
        return f"on weekdays at {r['time']}"
    if r["repeat"] == "weekly":
        return f"every {DAYS[r.get('weekday', 0)].title()} at {r['time']}"
    return spoken(datetime.fromtimestamp(r["next"]))


class Reminders:
    def __init__(self, path: Path | None, notifier: Notifier | None, bus: EventBus) -> None:
        self.path, self.notifier, self.bus = path, notifier, bus
        self.items: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        try:
            saved = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else []
        except (OSError, ValueError):
            saved = []
        self.items = {r["id"]: r for r in saved if isinstance(r, dict) and r.get("id") and r.get("next")}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            write_atomic(self.path, json.dumps(list(self.items.values()), indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("reminders not saved: %s", e)

    def add(self, text: str, at: datetime, repeat: str, to: list[str], weekday: int | None = None) -> dict:
        r = {"id": uuid.uuid4().hex[:6], "text": text, "time": f"{at:%H:%M}", "repeat": repeat,
             "to": to, "next": at.timestamp(), **({"weekday": weekday} if weekday is not None else {})}
        self.items[r["id"]] = r
        self._save()
        self._state("set", r)
        return r

    def all(self) -> list[dict]:
        return sorted(self.items.values(), key=lambda r: r["next"])

    def cancel(self, key: str) -> list[dict]:
        key = key.strip().lower()
        gone = [r for r in self.items.values() if key and (key == r["id"] or key in r["text"].lower())]
        for r in gone:
            self.items.pop(r["id"], None)
            self._state("cancelled", r)
        if gone:
            self._save()
        return gone

    def _state(self, kind: str, r: dict) -> None:
        for src in (Source.WEB, Source.PHONE):
            self.bus.publish(Event("reminder_state", src, "", {"kind": kind, **r, "when": describe(r)}))

    async def check(self, now: datetime | None = None) -> list[dict]:
        """Show every reminder that is due, then move it on (or drop a one-off). Returns those shown."""
        now = now or datetime.now()
        fired = []
        for r in sorted(self.items.values(), key=lambda r: r["next"]):
            if r["next"] > now.timestamp():
                continue
            late = now.timestamp() - r["next"]
            if r["repeat"] == "once" or late < CATCH_UP:
                await self._show(r, late > 300)
                fired.append(r)
            nxt = next_time(r, now)
            if nxt is None:
                self.items.pop(r["id"], None)
            else:
                r["next"] = nxt.timestamp()
        if fired or any(r["next"] <= now.timestamp() for r in self.items.values()):
            self._save()
        return fired

    async def _show(self, r: dict, late: bool) -> None:
        text = r["text"] + (" (late: JARVIS was offline)" if late else "")
        if self.notifier:
            await self.notifier.send("Reminder", text, r["to"] or DEFAULT_TO, origin="reminder")
        self.bus.publish(Event("reminder", Source.WEB, "", {"id": r["id"], "text": text, "repeat": r["repeat"]}))

    def start(self) -> None:
        async def loop() -> None:
            while True:
                try:
                    await self.check()
                except Exception:  # a failing notification must not stop the reminders
                    log.exception("reminder check failed")
                await asyncio.sleep(CHECK_S)
        self._task = asyncio.get_running_loop().create_task(loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


def register_reminder_tools(reg: ToolRegistry, reminders: Reminders) -> None:
    async def set_reminder(args: dict, ctx: ToolCtx) -> str:
        text = str(args.get("text") or "").strip()[:200]
        if not text:
            return "ERROR: say what to remind the user about (text)"
        repeat = str(args.get("repeat") or "once").strip().lower()
        repeat = {"every day": "daily", "day": "daily", "weekday": "weekdays", "week": "weekly", "no": "once",
                  "none": "once", "never": "once"}.get(repeat, repeat)
        if repeat not in REPEATS:
            return f"ERROR: repeat must be one of {', '.join(REPEATS)}"
        day_text, now = str(args.get("day") or ""), datetime.now()
        at = when(str(args.get("time") or ""), day_text, now)
        if at is None:
            return "ERROR: give the time as a clock time, e.g. 10:00, 10 am or 6:30 pm"
        weekday = None
        if repeat == "weekly":
            weekday = next((i for i, d in enumerate(DAYS) if d in day_text.lower()), at.weekday())
            at = next_time({"time": f"{at:%H:%M}", "repeat": "weekly", "weekday": weekday}, now - timedelta(minutes=1))
        elif repeat != "once" and at <= now:
            at = next_time({"time": f"{at:%H:%M}", "repeat": repeat}, now)
        elif repeat == "weekdays" and at.weekday() >= 5:
            at = next_time({"time": f"{at:%H:%M}", "repeat": repeat}, at - timedelta(minutes=1))
        if at <= now:
            return "ERROR: that time has already passed; give a later time or a day"
        to = targets_from(args.get("to")) or DEFAULT_TO
        r = reminders.add(text, at, repeat, to, weekday)
        where = " and ".join({"android": "your phone", "hud": "the HUD", "windows": "the laptop",
                              "voice": "the speakers"}[t] for t in to)
        start = f", starting {spoken(at, now)}" if repeat != "once" else ""
        # the reply as it stands (final_answer): when and where come from here, not from the model
        return f"I'll remind you to {text.rstrip('.')} {describe(r)}{start}, on {where}, sir."

    async def list_reminders(args: dict, ctx: ToolCtx) -> str:
        items = reminders.all()
        if not items:
            return "No reminders are set."
        return "\n".join(f"{r['text']}: {describe(r)}" for r in items)

    async def cancel_reminder(args: dict, ctx: ToolCtx) -> str:
        gone = reminders.cancel(str(args.get("match") or ""))
        if gone:
            return "Cancelled the reminder" + ("s" if len(gone) > 1 else "") + ": " + \
                "; ".join(f"{r['text']} ({describe(r)})" for r in gone) + "."
        listed = "; ".join(r["text"] for r in reminders.all())
        return (f"ERROR: no reminder matches that. Set: {listed}. Call cancel_reminder with a few words of one."
                if listed else "ERROR: no reminders are set, so there is nothing to cancel.")

    def known_text(args: dict) -> dict | None:  # "the acrylic reminder" -> the reminder it clearly means
        hit = best_name(str(args.get("match", "")), [r["text"] for r in reminders.all()])
        return {**args, "match": hit} if hit else None
    resolver("cancel_reminder", known_text)

    reg.add(Tool("set_reminder", "builtin",
                 "Remind the user at a time and day, once or repeating, on their phone and the HUD. Use for: 'remind me "
                 "every morning at 10 to follow up', 'remind me tomorrow at 9 to call the bank', 'remind me on Friday at "
                 "6 pm', 'a daily reminder at 8'. Not for a countdown in minutes (use set_timer).",
                 {"type": "object", "properties": {
                     "text": {"type": "string", "description": "what to remind them about, in their words"},
                     "time": {"type": "string", "description": "clock time, e.g. 10:00 or 6:30 pm"},
                     "day": {"type": "string", "description": "today, tomorrow, a weekday or a date; empty = next time"},
                     "repeat": {"type": "string", "enum": list(REPEATS)},
                     "to": {"type": "string", "description": "where: phone, hud, laptop, voice or all (default phone and hud)"}},
                  "required": ["text", "time"]},
                 Risk.WRITE, set_reminder, self_scoped=True, final_answer=True))
    reg.add(Tool("list_reminders", "builtin", "The reminders that are set. Use for: 'what reminders do I have'.",
                 {"type": "object", "properties": {}, "required": []}, Risk.READ, list_reminders))
    reg.add(Tool("cancel_reminder", "builtin",
                 "Cancel a reminder. Use for: 'cancel the acrylic reminder', 'stop reminding me to call the bank'.",
                 {"type": "object", "properties": {"match": {"type": "string", "description": "a few words of it"}},
                  "required": ["match"]}, Risk.WRITE, cancel_reminder, self_scoped=True))
