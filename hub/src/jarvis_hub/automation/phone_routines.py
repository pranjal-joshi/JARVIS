"""Phone routines: the user does something by hand on the phone while JARVIS records it, and it becomes a skill
that replays the same taps, swipes and keys later, without a model (bridge/phone_routine.py does the work).

A routine is a skill page with `replay: phone`, its steps one per line ("tap 540 1200", "wait 1.5s"):

    ---
    trigger: manual
    replay: phone
    ---
    - open com.frameo.app
    - tap 540 1210

Running it (run_skill, the HUD's run button, a trigger) goes through the fast path to phone_replay, which asks for
approval like any tap on the phone would. A routine page edited outside JARVIS waits to be enabled in the HUD, as
every skill does."""

import re

from jarvis_hub.approval.gate import Risk
from jarvis_hub.automation.skills import NS, SkillEngine
from jarvis_hub.devices.tools import bridge_call
from jarvis_hub.kb.graph import ObsidianVault, frontmatter
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

REPLAY_TIMEOUT_S = 600  # a long routine with waits; the bridge stops at 200 steps


def routine_steps(page) -> list[str]:
    return [b.text.split("\n")[0].strip() for b in page.blocks if b.text.strip()]


def register_phone_routine_tools(reg: ToolRegistry, engine: SkillEngine, graph: ObsidianVault) -> None:
    s = engine.settings

    async def start(args: dict, ctx: ToolCtx) -> str:
        ok, out = await bridge_call(s, "adb_record_start", {"device": args.get("device") or ""}, timeout=30)
        return str(out) if ok else f"ERROR: {out}"

    async def stop(args: dict, ctx: ToolCtx) -> str:
        name = re.sub(r"[^\w' &-]", "", str(args.get("name") or "")).strip()[:60]
        if len(name) < 2:
            return "ERROR: give the routine a short name, e.g. Frameo kiosk"
        page = NS + name
        if graph.page(page) is not None:
            return f"ERROR: there's already a skill called {name!r}; pick another name"
        ok, out = await bridge_call(s, "adb_record_stop", {}, timeout=30)
        if not ok:
            return f"ERROR: {out}"
        steps = [x for x in (out or {}).get("steps") or [] if isinstance(x, str)]
        if not any(x.startswith(("tap", "swipe", "key")) for x in steps):
            return "I didn't see any taps or swipes on the phone, sir, so there's nothing to save."
        graph.replace_page(page, frontmatter({"trigger": "manual", "replay": "phone"}) + "\n"
                           + "".join(f"- {x}\n" for x in steps))
        engine.set_enabled(page, True, pin=True)  # JARVIS wrote it from what the user did: pinned and on
        taps = sum(x.startswith(("tap", "swipe", "key")) for x in steps)
        return (f"Saved the routine '{name}', sir: {taps} taps and swipes over {out.get('seconds', '?')} seconds. "
                f"Say 'run the {name} skill' to replay it.")

    async def replay(args: dict, ctx: ToolCtx) -> str:
        want = str(args.get("name") or "").strip().lower()
        skill = next((x for x in engine.skills() if x["name"].lower() in (want, NS + want)), None)
        page = graph.page(skill["name"]) if skill else None
        if not page or page.properties.get("replay", "").lower() != "phone":
            return f"ERROR: there's no recorded phone routine called {args.get('name')!r}"
        if not engine.live(skill):
            return "ERROR: that routine was changed outside JARVIS or is off; enable it in the HUD first"
        ok, out = await bridge_call(s, "adb_replay", {"steps": routine_steps(page), "device": args.get("device") or ""},
                                    timeout=REPLAY_TIMEOUT_S)
        return str(out) if ok else f"ERROR: {out}"

    dev = {"device": {"type": "string", "description": "which phone, if several are connected"}}
    reg.add(Tool("phone_record_start", "builtin",
                 "Start recording what the user does by hand on the phone, to save as a routine. "
                 "Use for: 'watch what I do on my phone', 'record a phone routine'.",
                 {"type": "object", "properties": dev, "required": []}, Risk.WRITE, start, final_answer=True))
    reg.add(Tool("phone_record_stop", "builtin",
                 "Stop recording the phone and save what the user did as a routine (a skill) with this name. "
                 "Use for: 'stop recording, call it Frameo kiosk'.",
                 {"type": "object", "properties": {"name": {"type": "string", "description": "the routine's name"}},
                  "required": ["name"]}, Risk.WRITE, stop, final_answer=True))
    reg.add(Tool("phone_replay", "builtin",
                 "Replay a recorded phone routine: the same taps and swipes on the phone. Needs approval.",
                 {"type": "object", "properties": {"name": {"type": "string"}, **dev}, "required": ["name"]},
                 Risk.DESTRUCTIVE, replay, final_answer=True))
