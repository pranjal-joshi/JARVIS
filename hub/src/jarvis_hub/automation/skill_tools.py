"""Tools for making and changing skills by talking to JARVIS: "make a skill that says hello world", "change the
morning skill to run at 8", "run the hello world skill", "delete that skill".

A skill is on as soon as it exists (see skills.py); switching one off or on needs no approval, deleting asks
first (a copy is kept in the trash). run_skill waits for the run and gives its answer as the reply."""

import asyncio
import re
import time

from jarvis_hub.approval.gate import Risk
from jarvis_hub.automation.skills import DAILY, EVERY, NS, RUN_SOURCE, TRIGGERS, SkillEngine
from jarvis_hub.events.bus import Event
from jarvis_hub.guard.validate import best_name, resolver
from jarvis_hub.kb.graph import LogseqGraph
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

RUN_WAIT_S = 90  # run_skill waits this long for the answer; a longer run's answer shows on the HUD
TRIGGER_HELP = ("manual (only when asked: 'run the X skill'), schedule (with schedule like 07:30 or every 2h), "
                "device_joined, device_left, timer_done, notification, call_incoming, watch, "
                "webhook (with match = the hook name)")


def _page(name: str) -> str:
    """"hello world" -> "skills/Hello world"; a name already under skills/ stays."""
    name = name.strip().strip("/")
    if name.lower().startswith(NS):
        name = name[len(NS):]
    return NS + (name[:1].upper() + name[1:])


def _steps(instructions: str) -> list[str]:
    """One step per line, without the bullets or numbers a model puts in front ("1. ", "- ")."""
    out = []
    for line in instructions.splitlines():
        step = re.sub(r"^(?:[-*•]\s*|\d{1,2}[.)]\s+)+", "", line.strip()).strip()
        step = re.sub(r"^steps?\s*:\s*", "", step, flags=re.I).strip()  # "Steps: tell me..." -> "tell me..."
        if step:
            out.append(step)
    return out


def _text(trigger: str, match: str, schedule: str, steps: list[str]) -> str:
    """A skill page: its properties, then each step as a block."""
    props = [f"trigger:: {trigger}"] + ([f"match:: {match}"] if match else []) + \
        ([f"schedule:: {schedule}"] if schedule else [])
    return "\n".join(props) + "\n\n" + "\n".join(f"- {s}" for s in steps) + "\n"


def _listed(steps: list[str]) -> str:
    return " ".join(f"({i}) {s.rstrip('.')}." for i, s in enumerate(steps, 1))


# "Run on webhook doorbell." / "Runs when the webhook doorbell fires": a small model puts the trigger in the
# steps instead of trigger/match, which leaves a manual skill no webhook can start
WEBHOOK_STEP = re.compile(
    r"^(?:it\s+|this\s+(?:skill\s+)?)?(?:runs?|triggers?|fires?|starts?|trigger(?:ed)?)?\s*(?:on|when|from|via|by|for)\s+"
    r"(?:a\s+|the\s+)?webhook\s+(?:hook\s+)?(?:called\s+|named\s+)?[\"'“]?([a-z0-9_-]{1,40})[\"'”]?"
    r"(?:\s+(?:fires|is\s+called|arrives|comes\s+in))?\s*[.!]?$", re.I)


def _webhook_from_steps(steps: list[str]) -> tuple[str, list[str]]:
    """The hook name a step names as the trigger (and the steps without it), or "" when none does."""
    for i, step in enumerate(steps):
        if m := WEBHOOK_STEP.match(step.strip()):
            return m[1].lower(), steps[:i] + steps[i + 1:]
    return "", steps


def _check(trigger: str, schedule: str, steps: list[str]) -> str | None:
    if trigger not in TRIGGERS:
        return f"ERROR: trigger must be one of: {TRIGGER_HELP}"
    if trigger == "schedule" and not (EVERY.match(schedule) or DAILY.match(schedule)):
        return "ERROR: a scheduled skill needs schedule, a daily time like 07:30 or an interval like every 30m"
    if not steps:
        return "ERROR: say what the skill should do (instructions, one step per line)"
    return None


def _state(s: dict) -> str:
    return "on" if s["enabled"] else "switched off"


def _when(s: dict) -> str:
    if s["trigger"] == "schedule":
        return f"runs {s['schedule'] if s['schedule'].startswith('every') else 'daily at ' + s['schedule']}"
    if s["trigger"] in ("manual", ""):
        return "runs when asked"
    return f"runs on {s['trigger']}" + (f" matching {s['match']}" if s["match"] else "")


def register_skill_tools(reg: ToolRegistry, engine: SkillEngine, graph: LogseqGraph) -> None:
    def find(name: str) -> dict | None:
        skills = engine.skills()
        names = [s["name"] for s in skills]
        hit = best_name(str(name), names) or best_name(_page(str(name)), names) or \
            best_name(str(name), [n[len(NS):] for n in names])
        if hit and not hit.lower().startswith(NS):
            hit = NS + hit
        return next((s for s in skills if s["name"].lower() == (hit or "").lower()), None)

    def missing(name: str) -> str:
        names = ", ".join(s["name"][len(NS):] for s in engine.skills())
        return (f"ERROR: no skill called {name!r}. Skills: {names}." if names
                else "ERROR: there are no skills yet; make one with create_skill.")

    async def list_skills(args: dict, ctx: ToolCtx) -> str:
        skills = engine.skills()
        if not skills:
            return "There are no skills yet, sir. Say 'make a skill called ... that ...' to make one."
        each = [f"{s['name'][len(NS):]} ({_when(s)}, {_state(s)}): {_listed(_steps(s['instructions']))[:200]}"
                for s in skills]
        return f"You have {len(skills)} skill{'s' * (len(skills) != 1)}, sir. " + " ".join(each)

    async def create_skill(args: dict, ctx: ToolCtx) -> str:
        name = str(args.get("name") or "").strip()
        if len(name) < 2:
            return "ERROR: give the skill a short name"
        trigger = str(args.get("trigger") or "manual").strip().lower()
        schedule, match = str(args.get("schedule") or "").strip(), str(args.get("match") or "").strip()
        steps = _steps(str(args.get("instructions") or ""))
        if trigger in ("manual", "webhook"):
            hook, rest = _webhook_from_steps(steps)
            if hook and (trigger == "manual" or not match or match.lower() == hook):
                trigger, match, steps = "webhook", hook, rest
        if err := _check(trigger, schedule, steps):
            return err
        page = _page(name)
        if graph.page(page) is not None:
            return f"ERROR: there's already a skill called {page[len(NS):]!r}; change it with update_skill"
        graph.replace_page(page, _text(trigger, match, schedule, steps))
        engine.set_enabled(page, True)  # a name switched off before and made again starts on
        when = _when({"trigger": trigger, "schedule": schedule, "match": match})
        return f"Made the skill '{page[len(NS):]}', sir; it {when} and is on. Steps: {_listed(steps)}"

    async def update_skill(args: dict, ctx: ToolCtx) -> str:
        s = find(args.get("name", ""))
        if s is None:
            return missing(str(args.get("name", "")))
        if not any(args.get(k) for k in ("instructions", "add_steps", "trigger", "schedule", "match")):
            return ("ERROR: say what to change: instructions (all the new steps), add_steps (steps to add at the "
                    f"end), trigger, schedule or match. Its steps now: {_listed(_steps(s['instructions']))}")
        added = _steps(str(args.get("add_steps") or ""))
        # with a step to add, instructions is ignored: a model adding a step tends to also put a placeholder
        # ("[the previous instructions] + ...") into it
        steps = (None if added else _steps(str(args.get("instructions") or ""))) or _steps(s["instructions"])
        steps += [a for a in added if a not in steps]
        trigger = str(args.get("trigger") or s["trigger"] or "manual").strip().lower()
        schedule = str(args.get("schedule") or (s["schedule"] if trigger == "schedule" else "")).strip()
        match = str(args["match"] if args.get("match") is not None else s["match"]).strip()
        if not args.get("trigger") or trigger == "webhook":
            hook, rest = _webhook_from_steps(steps)
            if hook and (trigger in ("manual", "webhook")):
                trigger, match, steps = "webhook", hook, rest
        if err := _check(trigger, schedule, steps):
            return err
        graph.replace_page(s["name"], _text(trigger, match, schedule, steps))
        when = _when({"trigger": trigger, "schedule": schedule, "match": match})
        return f"Changed the skill '{s['name'][len(NS):]}', sir; it {when}. Steps now: {_listed(steps)}"

    async def delete_skill(args: dict, ctx: ToolCtx) -> str:
        s = find(args.get("name", ""))
        if s is None:
            return missing(str(args.get("name", "")))
        graph.trash_page(s["name"])
        if s["name"].lower() in engine.disabled:  # a skill made again under this name starts on
            engine.disabled.discard(s["name"].lower())
            engine._save()
        return f"Deleted the skill '{s['name'][len(NS):]}' (a copy is in the trash), sir."

    async def enable_skill(args: dict, ctx: ToolCtx) -> str:
        s = find(args.get("name", ""))
        if s is None:
            return missing(str(args.get("name", "")))
        on = args.get("enabled", True) not in (False, "false", "no", "off")
        engine.set_enabled(s["name"], on)
        return f"Switched the skill '{s['name'][len(NS):]}' {'on' if on else 'off'}, sir."

    async def run_skill(args: dict, ctx: ToolCtx) -> str:
        s = find(args.get("name", ""))
        if s is None:
            return missing(str(args.get("name", "")))
        name = s["name"][len(NS):]
        if not s["enabled"]:
            return f"The skill '{name}' is switched off, sir; say 'turn the {name} skill back on' first."
        if s["name"].lower() in engine._running:
            return f"The skill '{name}' is already running, sir; its answer will show on the HUD."
        engine.last_run[s["name"].lower()] = time.time()
        task = asyncio.get_running_loop().create_task(engine.run(s, "asked to run it now", announce=False))
        done, _ = await asyncio.wait({task}, timeout=RUN_WAIT_S)
        if done:
            return task.result()

        def late(t: asyncio.Task) -> None:  # the answer, when it comes, goes to the HUD
            if not t.cancelled() and t.exception() is None:
                engine.bus.publish(Event("skill_done", RUN_SOURCE, ctx.request_id,
                                         {"skill": s["name"], "text": t.result(), "dry_run": False, "would": []}))
        task.add_done_callback(late)
        return f"The skill '{name}' is taking a while, sir; its answer will show on the HUD."

    def known(args: dict) -> dict | None:  # "the hello skill" -> the skill it clearly means
        s = find(args.get("name", ""))
        return {**args, "name": s["name"]} if s else None
    for name in ("update_skill", "delete_skill", "enable_skill", "run_skill"):
        resolver(name, known)

    named = {"name": {"type": "string", "description": "the skill's name, e.g. hello world"}}
    shape = {"trigger": {"type": "string", "enum": sorted(TRIGGERS),
                         "description": f"when it runs: {TRIGGER_HELP}"},
             "schedule": {"type": "string", "description": "for trigger schedule: 07:30 or every 30m"},
             "match": {"type": "string", "description": "optional: words the event must contain (a phone model, timer label); for trigger webhook: the hook name, e.g. doorbell"},
             "instructions": {"type": "string", "description": "the steps, one per line, each a request to JARVIS in "
                                                               "the user's words, e.g. 'Say good morning.\\nTell me the "
                                                               "time.' For update_skill: all the new steps (they replace "
                                                               "the old); leave it out when adding a step"}}
    reg.add(Tool("list_skills", "builtin", "The skills (automations) JARVIS has, their steps, when each runs, whether on.",
                 {"type": "object", "properties": {}, "required": []}, Risk.READ, list_skills, final_answer=True))
    reg.add(Tool("create_skill", "builtin",
                 "Make a new skill: steps JARVIS carries out when asked (run_skill) or on a trigger. It is on at once.",
                 {"type": "object", "properties": {**named, **shape}, "required": ["name", "instructions"]},
                 Risk.WRITE, create_skill, final_answer=True))
    reg.add(Tool("update_skill", "builtin",
                 "Change a skill: add_steps adds steps at the end; instructions replaces all its steps; or a new "
                 "trigger or schedule. Give only what changes.",
                 {"type": "object", "properties": {**named, **shape,
                  "add_steps": {"type": "string", "description": "steps to add at the end, one per line"}},
                  "required": ["name"]},
                 Risk.WRITE, update_skill, final_answer=True))
    reg.add(Tool("delete_skill", "builtin", "Delete a skill (a copy is kept in the trash).",
                 {"type": "object", "properties": named, "required": ["name"]}, Risk.DESTRUCTIVE, delete_skill,
                 final_answer=True))
    reg.add(Tool("enable_skill", "builtin", "Switch a skill off, or back on.",
                 {"type": "object", "properties": {**named,
                  "enabled": {"type": "boolean", "description": "true = on (default), false = off"}},
                  "required": ["name"]},
                 Risk.WRITE, enable_skill, final_answer=True))
    reg.add(Tool("run_skill", "builtin", "Run a skill now and say what it gives.",
                 {"type": "object", "properties": named, "required": ["name"]}, Risk.WRITE, run_skill,
                 final_answer=True))
