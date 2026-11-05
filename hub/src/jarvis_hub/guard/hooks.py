"""Hooks around every tool call: the checks that used to be scattered through the agent loop, in one
place with one shape, so new ones are a function, not a change to the loop.

  pre_tool(tool, args, turn)  -> None (fine) | Verdict("error", msg) (back to the model)
                                           | Verdict("ask", why) (needs a human, whatever the policy says)
  post_tool(tool, args, output, turn) -> output   (may record or redact; the model sees the result)

Taint: text from outside (web pages, notifications, messages, files) is recorded as it arrives. A
write or send whose arguments carry that text needs a human, so a web page or a message can't
quietly become an action or a memory: the one thing that makes wider read access safe."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from jarvis_hub.approval.gate import Risk
from jarvis_hub.guard.validate import check
from jarvis_hub.tools.when import clock, day_words, repeat_of

# tools whose output is someone else's words
TAINT_SOURCES = {"web_search", "fetch_page", "adb_notifications", "adb_sms_read", "adb_call_log", "fs_read",
                 "fs_list", "win_ps_read", "win_ps_run"}
# reads whose full text shouldn't sit in the audit log (it keeps a short note instead)
REDACT_IN_AUDIT = {"adb_notifications", "adb_sms_read", "adb_call_log", "fs_read"}

# What makes a web search worth it: the user asked for one, or wants something current.
CURRENT = re.compile(
    r"\b(search|look (?:it |that )?up|google|web|online|internet|latest|news|today|tonight|yesterday|tomorrow|"
    r"current(?:ly)?|now|recent(?:ly)?|this (?:week|month|year|season)|price|cost|rate|score|stock|"
    r"open|hours|release[ds]?|update[ds]?|live|20\d\d|who won|result)\b|https?://|\bwww\.", re.I)
# What a 2B model does know: textbook facts, meanings, everyday advice. Anything else (a tool, a product,
# a name it hasn't seen: "what is Ghidra") it makes up when it can't search, so only these are refused.
TEXTBOOK = re.compile(
    r"\b(?:capital|population|currency|language) of\b|\b(?:mean|means|meaning|define|definition|synonym|antonym|"
    r"spell|spelling|translate)\b|\b(?:tips?|advice)\b|\bwho (?:wrote|painted|invented|discovered)\b"
    r"|\b(?:joke|poem|story)\b", re.I)
# Notes JARVIS writes: only when the user's latest message asks for it, and only in words the user said or a
# tool returned. A 2B model whose search was refused wrote its own (wrong) description of Ghidra into a page,
# and journaled the conversation unasked.
NOTE_WRITES = {"kb_journal", "kb_append_to_page"}
NOTE_TOOLS = NOTE_WRITES | {"kb_remember"}
ASKS_TO_SAVE = re.compile(r"\b(?:save|note|jot|journal|log|add|put|write|remember|record|keep|store|track)\b", re.I)
_PLAIN = set("""about after again also been before being from have here into just like more much only over said
some than that their them then there these they this those very what when where which while will with would your
user user's today sir jarvis""".split())
# What makes get_time worth calling: the question is about a time, a date or a day.
ABOUT_TIME = re.compile(
    r"\b(time|clock|o'?clock|date\w*|timestamp|day|days|today|tonight|tomorrow|yesterday|now|when|hour|hours|minute|minutes|"
    r"week|weekend|month|year|morning|afternoon|evening|night|late|early|until|till|ago|since|age|old|"
    r"birthday|anniversary|calendar|schedule|zone|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\b", re.I)

# Arguments a write may only contain if the user said them (or a tool returned them this turn):
# a 2B model "remembers" 9 June as September 9 and texts the wrong name.
SLOT_KEYS = ("fact", "text", "to", "name", "page", "message", "contact")
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_NOT_NAMES = {"User", "User's", "I", "I'm", "The", "Sir", "JARVIS", "Jarvis", "My", "Mr", "Mrs", "Ms", "Dr",
              "Remember", "Note", "Today", "Tomorrow", "Yesterday"}
_MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_WORDS = re.compile(r"[a-z0-9']+")
LINK = re.compile(r"https?://[^\s<>()\[\]\"'`]+[^\s<>()\[\]\"'`.,;:!?]")
SPAN = 5  # this many words in a row from outside text = carried over


@dataclass
class Verdict:
    kind: str  # "error" | "ask" | "fix" (args holds the corrected arguments; later hooks see those)
    message: str
    args: dict | None = None


def unsaid(tool, args: dict, heard: str, seen: list[str]) -> list[str]:
    """Names, numbers and months in a write's arguments that the user never said."""
    if tool.risk is Risk.READ or not heard:
        return []
    said = heard + " " + " ".join(seen)
    said_l = said.lower()
    digits = set(re.findall(r"\d+", said)) | {d for w, d in _NUMBER_WORDS.items() if re.search(rf"\b{w}\b", said_l)}
    months = {m.lower()[:3] for m in _MONTH.findall(said)}
    bad = []
    for key in SLOT_KEYS:
        value = args.get(key)
        if not isinstance(value, str):
            continue
        # "10:00" for "at 10": whole hours' zero minutes are no new number
        bad += [n for n in re.findall(r"\d+", value) if n not in digits and n.lstrip("0") not in digits and n != "00"]
        bad += [m[0] for m in _MONTH.finditer(value) if m[1].lower() not in months]
        for m in re.finditer(r"\b[A-Z][a-z]{2,}(?:'s)?\b", value):  # names; a sentence's first word is just a capital
            w, before = m[0], value[:m.start()].rstrip()
            if before and not before.endswith((".", "!", "?", ":")) and w not in _NOT_NAMES \
                    and not _MONTH.fullmatch(w) and w.removesuffix("'s").lower() not in said_l:
                bad.append(w)
    return list(dict.fromkeys(bad))


def carried(args: dict, tainted: list[str], heard: str) -> str:
    """The first run of outside words (from tainted text, not said by the user) found in the arguments."""
    if not tainted:
        return ""
    spans = set()
    for text in tainted:
        words = _WORDS.findall(text.lower())
        spans |= {" ".join(words[i:i + SPAN]) for i in range(len(words) - SPAN + 1)}
    said = " ".join(_WORDS.findall(heard.lower()))
    for value in args.values():
        if not isinstance(value, str):
            continue
        words = _WORDS.findall(value.lower())
        for i in range(len(words) - SPAN + 1):
            span = " ".join(words[i:i + SPAN])
            if span in spans and span not in said:
                return span
    return ""


# ---------- the hooks ----------
def arguments(tool, args, t) -> Verdict | None:
    """Types, enum spelling and live names (guard/validate.py), fixed where that needs no guessing."""
    fixed, error = check(tool, args)
    if error:
        return Verdict("error", error)
    if fixed is not None:
        t.trace.append({"tool": tool.name, "decision": "fixed", "rule": "arguments",
                        "why": ", ".join(f"{k}={fixed[k]!r}" for k in fixed if fixed.get(k) != args.get(k))})
        return Verdict("fix", "", fixed)
    return None


def needless_search(tool, args, t) -> Verdict | None:
    said = getattr(t, "said", "") or t.heard
    if tool.name == "web_search" and not CURRENT.search(t.heard) and TEXTBOOK.search(said):
        # a 2B model searches for "the capital of Australia": a credit, an approval and 20 s, for nothing
        return Verdict("error", "ERROR: that is general knowledge, not current news. Answer it yourself without "
                                "searching; if you really don't know, say so.")
    return None


def needless_time(tool, args, t) -> Verdict | None:
    if tool.name == "get_time" and not ABOUT_TIME.search(getattr(t, "said", "") or t.heard):
        # a 2B model checks the clock before "what's the capital of Australia"
        return Verdict("error", "ERROR: the user didn't ask about a time or date. Answer them without it.")
    return None


def clock_time(tool, args, t) -> Verdict | None:
    """The clock time, day and repeat the user said go into a timer or reminder as they said them; code works
    out the seconds (tools/when.py). A 2B model set "10 AM tomorrow" as a 12-hour countdown ending at 06:24."""
    if tool.name not in ("set_timer", "set_reminder"):
        return None
    said = getattr(t, "said", "") or ""
    hm, on = clock(said), day_words(said)
    fixed = dict(args)
    if tool.name == "set_timer" and hm:
        at = f"{hm[0]:02}:{hm[1]:02}" + (f" {on}" if on else "")
        if clock(str(args.get("at") or ""), True) != hm or day_words(str(args.get("at") or "")) != on:
            fixed = {k: v for k, v in args.items() if k not in ("minutes", "seconds")} | {"at": at}
    if tool.name == "set_reminder":
        if hm and clock(str(args.get("time") or ""), True) != hm:
            fixed["time"] = f"{hm[0]:02}:{hm[1]:02}"
        repeat, weekday = repeat_of(said)
        if repeat and args.get("repeat") != repeat:
            fixed["repeat"] = repeat
        if weekday and weekday not in str(args.get("day") or "").lower():
            fixed["day"] = weekday
        elif on and not repeat and not day_words(str(args.get("day") or "")):
            fixed["day"] = on
    if fixed == args:
        return None
    t.trace.append({"tool": tool.name, "decision": "fixed", "rule": "arguments",
                    "why": ", ".join(f"{k}={fixed.get(k)!r}" for k in fixed if fixed.get(k) != args.get(k))})
    return Verdict("fix", "", fixed)


def keep_links(tool, args, t) -> Verdict | None:
    """A note saved about a link the user gave (or a page just read) keeps the link: a 2B model wrote the
    article's gist and dropped its URL."""
    links = getattr(t, "links", None)
    key = "fact" if "fact" in args else "text"
    if tool.name not in NOTE_TOOLS or not links or not isinstance(args.get(key), str) \
            or any(u in args[key] for u in links):
        return None
    fixed = {**args, key: f"{args[key].rstrip()} ({' '.join(links[:2])})"}
    t.trace.append({"tool": tool.name, "decision": "fixed", "rule": "arguments", "why": "kept the link"})
    return Verdict("fix", "", fixed)


def own_words(tool, args, t) -> Verdict | None:
    if tool.name not in NOTE_TOOLS:
        return None
    said = getattr(t, "said", "") or t.heard
    if tool.name in NOTE_WRITES and not ASKS_TO_SAVE.search(said):
        return Verdict("error", "ERROR: the user didn't ask you to write anything down. Answer them without it.")
    text = " ".join(str(v) for k, v in args.items() if k in ("text", "fact") and isinstance(v, str))
    words = [w for w in _WORDS.findall(text.lower()) if len(w) > 3 and w not in _PLAIN]
    known = set(_WORDS.findall((t.heard + " " + " ".join(t.seen)).lower()))
    new = [w for w in words if w not in known]
    if words and len(new) > max(2, len(words) // 3):
        return Verdict("error", f"ERROR: {tool.model_name} would save words nobody said ({', '.join(new[:5])}). "
                                "Save only what the user said or a tool returned; never your own knowledge.")
    return None


def slots(tool, args, t) -> Verdict | None:
    if bad := unsaid(tool, args, t.heard, t.seen):
        return Verdict("error", f"ERROR: {tool.model_name} has {', '.join(bad)}, which the user didn't say. "
                                "Use the user's own words, names, dates and numbers, then call it again.")
    return None


def taint(tool, args, t) -> Verdict | None:
    # a notification only ever goes to the user, so showing them outside text is fine (it's the point of it)
    if tool.risk is not Risk.READ and tool.name != "notify" and (span := carried(args, t.tainted, t.heard)):
        return Verdict("ask", f'carries outside text ("{span}…") from a web page, message or file')
    return None


def record_taint(tool, args, output: str, t) -> str:
    if tool.name in TAINT_SOURCES or "__" in tool.name and tool.risk is Risk.READ and len(output) > 200:  # MCP reads
        t.tainted.append(output)
    return output


PRE_TOOL: list[Callable] = [arguments, needless_search, needless_time, clock_time, keep_links, own_words, slots, taint]
POST_TOOL: list[Callable] = [record_taint]


def audit_text(tool, output: str) -> str:
    """What the audit log keeps of a result: personal message text stays out of it."""
    if tool.name in REDACT_IN_AUDIT and not output.startswith("ERROR"):
        return f"({len(output)} characters, not kept in the audit)"
    return output[:2000]
