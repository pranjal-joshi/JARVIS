"""Skill requests in their usual words, turned into a skill tool call by code (agent/fastpath.py): "make a skill
called tea time that tells me to make tea", "add a step to the tea time skill: say the time", "change the tea
time skill so it only says good morning", "make the tea time skill run every day at 07:30", "run tea time",
"turn the tea time skill off", "delete the tea time skill", "what skills do you have".

A small local model got most of these wrong: it asked to clarify, put placeholders into the steps, or called
nothing and said it was done. Anything not matched here goes to the model as before. A skill is named in these
words only when it is one that exists (run, on, off, delete, change), so "run the dishwasher" is left alone."""

import re

from jarvis_hub.guard.validate import best_name
from jarvis_hub.tools.when import clock

_THE = r"(?:the |my |a |an )?"
_SKILL = r"(?: skill| automation)?"
# "greets me" -> "greet me": the steps are written as requests to JARVIS
_IRREGULAR = {"does": "do", "goes": "go", "has": "have", "is": "be", "says": "say"}
VERBS = {"say", "tell", "read", "give", "show", "remind", "play", "open", "send", "check", "set", "turn", "list",
         "ask", "find", "search", "look", "make", "start", "stop", "pause", "call", "text", "message", "note", "add",
         "remember", "greet", "wish", "report", "summarise", "summarize", "lock", "mute", "get", "put", "write",
         "announce", "notify", "speak", "count", "do", "go"}


def _imperative(clause: str) -> str:
    words = clause.strip().rstrip(".!").split(" ")
    first = words[0].lower() if words and words[0] else ""
    if first in _IRREGULAR:
        words[0] = _IRREGULAR[first]
    elif first.endswith("ies") and len(first) > 4:
        words[0] = first[:-3] + "y"
    elif re.search(r"(?:sh|ch|x|ss|z)es$", first):
        words[0] = first[:-2]
    elif first.endswith("s") and not first.endswith("ss") and len(first) > 3:
        words[0] = first[:-1]
    out = " ".join(words).strip()
    return (out[:1].upper() + out[1:] + ".") if out else ""


def steps_of(clause: str) -> list[str]:
    """"greets me and tells me today's date" -> ["Greet me.", "Tell me today's date."]; "says good morning and the
    time" stays one step (the part after "and" is no request of its own)."""
    clause = re.sub(r"^(?:it |that it |which )?(?:will |should |can )?(?:only |just )?", "", clause.strip(), flags=re.I)
    parts = re.split(r"\s*(?:,\s*and then|,\s*then|;|\band then\b|\bthen\b|\band also\b|\.\s+)\s*", clause)
    out = []
    for part in parts:
        halves = re.split(r"\s+and\s+", part)
        # "greets me and tells me the date": two requests; "says good morning and the time": one
        if len(halves) > 1 and all(_imperative(h).split(" ")[0].lower() in VERBS for h in halves[1:]):
            out += halves
        else:
            out.append(part)
    return [s for s in (_imperative(p) for p in out if p.strip()) if s]


def _schedule(text: str) -> str | None:
    """"every day at 7:30" / "daily at 7 am" -> "07:30"; "every 2 hours" -> "every 2h"; "every 30 minutes" -> "every 30m"."""
    if m := re.search(r"\bevery (\d{1,3}) ?(minutes?|mins?|m|hours?|hrs?|h)\b", text, re.I):
        return f"every {int(m[1])}{'h' if m[2].lower().startswith('h') else 'm'}"
    if m := re.search(r"\bevery (minute|hour)\b", text, re.I):
        return "every 1h" if m[1].lower() == "hour" else "every 1m"
    if re.search(r"\b(?:every (?:day|morning|evening|night)|daily|each day)\b", text, re.I) and (at := clock(text, True)):
        return f"{at[0]:02d}:{at[1]:02d}"
    return None


def skill_plan(text: str, names: list[str]) -> tuple[str, dict] | None:
    """(tool, arguments) for a skill request, or None. text: the request as said (case kept); names: the skills
    there are, without "skills/"."""
    t = text.strip().rstrip("?.! ")

    def known(spoken: str) -> str | None:
        spoken = re.sub(r"^(?:the|my) ", "", spoken.strip(), flags=re.I)
        spoken = re.sub(r" (?:skill|automation)$", "", spoken, flags=re.I).strip()
        hit = best_name(spoken, names) if spoken else None
        # a name inside a longer request isn't the skill: "run a job to print hello world from the job" ran the
        # hello world skill
        return hit if hit and len(spoken.split()) <= len(hit.split()) + 2 else None

    # the one skill named anywhere in the request: "I don't need the fun fact automation any more, get rid of it"
    said = [n for n in names if re.search(rf"\b{re.escape(n)}\b", t, re.I)]
    mentioned = said[0] if len(said) == 1 else None

    if re.fullmatch(r"(?:what|which) (?:skills|automations)(?: do you have| are there| have i got| do i have)?"
                    r"|(?:list|show)(?: me)? (?:my |the |all )?(?:skills|automations)|(?:my|any) skills", t, re.I):
        return "list_skills", {}

    m = re.fullmatch(r"(?:make|create|add|set up|build)(?: me)? (?:a |an )?(?:new )?(?:skill|automation)(?: for me)?,?"
                     r"(?: (?:called|named|call it|name it))? ['\"“]?(.{2,40}?)['\"”]?,? (?:that|which|to|so it|so that it)"
                     r" (.{3,400})", t, re.I)
    if m:
        name, how = m[1].strip(), m[2]
        sched = _schedule(how)
        how = re.sub(r",? *(?:every (?:day|morning|evening|night)|daily|each day)(?: at [^,.]+)?|,? *every \d+ ?\w+",
                     "", how, flags=re.I) if sched else how
        if not (steps := steps_of(how)):
            return None
        args = {"name": name, "instructions": "\n".join(steps)}
        return "create_skill", {**args, "trigger": "schedule", "schedule": sched} if sched else args

    m = re.fullmatch(rf"(?:add|put) (?:a |another |one more )?(?:step|line)? ?(?:to|in|into) {_THE}(.+?){_SKILL}\s*[:,-]\s*(.+)"
                     rf"|(?:add|put) (.+?) (?:to|in|into) {_THE}(.+?) skill", t, re.I)
    if m:
        name, step = (m[1], m[2]) if m[1] else (m[4], m[3])
        if (hit := known(name)) and (steps := steps_of(re.sub(r"^(?:also )?", "", step, flags=re.I))):
            return "update_skill", {"name": hit, "add_steps": "\n".join(steps)}
        return None

    m = re.fullmatch(rf"(?:make|set|have|change|schedule) {_THE}(.+?){_SKILL} (?:to )?(?:run|go off|start|happen|fire)? ?"
                     r"(every .+|daily .+|each day .+|at .+ every day|at .+ daily)", t, re.I)
    if m and (hit := known(m[1])) and (sched := _schedule(m[2])):
        return "update_skill", {"name": hit, "trigger": "schedule", "schedule": sched}

    m = re.fullmatch(rf"(?:change|update|edit|rewrite) {_THE}(.+?){_SKILL} (?:so (?:that )?it|to|so it'?ll) (.{{3,400}})", t, re.I)
    if m and (hit := known(m[1])) and (steps := steps_of(m[2])):
        return "update_skill", {"name": hit, "instructions": "\n".join(steps)}

    m = re.fullmatch(rf"(?:turn|switch) (?:off|on) {_THE}(.+?){_SKILL}|(?:turn|switch) {_THE}(.+?){_SKILL} (?:back )?(off|on)"
                     rf"|(disable|enable|pause|resume|stop) {_THE}(.+?){_SKILL}", t, re.I)
    if m:
        spoken = m[1] or m[2] or m[5]
        if m[1]:
            on = bool(re.match(r"(?:turn|switch) on", t, re.I))
        elif m[2]:
            on = m[3].lower() == "on"
        else:
            on = m[4].lower() in ("enable", "resume")
        if hit := known(spoken):
            return "enable_skill", {"name": hit, "enabled": on}
        return None

    m = re.fullmatch(rf"(?:delete|remove|get rid of) {_THE}(.+?){_SKILL}", t, re.I)
    if m and (hit := known(m[1])) and (re.search(r"skill|automation", t, re.I) or hit.lower() == m[1].strip().lower()):
        return "delete_skill", {"name": hit}
    if mentioned and re.search(r"\b(?:delete|remove|get rid of|bin|drop)\b", t, re.I) and \
            re.search(r"\b(?:skill|automation|it|that)\b", t, re.I):
        return "delete_skill", {"name": mentioned}

    # "for the fun fact skill, make it also mention who discovered it", "have the X skill also say the weather"
    m = re.search(rf"(?:for|in|to) {_THE}(.+?){_SKILL},? (?:make it|have it|get it|let it)? ?also (.+)"
                  rf"|(?:make|have|get|let) {_THE}(.+?){_SKILL} also (.+)", t, re.I)
    if m:
        hit = known(m[1] or m[3]) or mentioned
        if hit and (steps := steps_of(m[2] or m[4])):
            return "update_skill", {"name": hit, "add_steps": "\n".join(steps)}

    m = re.fullmatch(rf"(?:run|try|start|do|play|execute) {_THE}(.+?){_SKILL}(?: (?:now|again|for me))?", t, re.I)
    if m and (hit := known(m[1])):
        return "run_skill", {"name": hit}
    # "fun fact, go", "hello world now please"
    m = re.fullmatch(r"(.+?),? (?:go|now|please|run it|go ahead|do it|start)", t, re.I)
    if m and (hit := known(m[1])) and hit.lower() == m[1].strip().lower():
        return "run_skill", {"name": hit}
    return None
