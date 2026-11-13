"""Hooks around every tool call: the checks that used to be scattered through the agent loop, in one
place with one shape, so new ones are a function, not a change to the loop.

  pre_tool(tool, args, turn)  -> None (fine) | Verdict("error", msg) (back to the model)
                                           | Verdict("ask", why) (needs a human, whatever the policy says)
  post_tool(tool, args, output, turn) -> output   (may record or redact; the model sees the result)

Taint: text from outside (web pages, notifications, messages, files) is recorded as it arrives. A
write or send whose arguments carry that text needs a human, so a web page or a message can't
quietly become an action or a memory: the one thing that makes wider read access safe.

Outside text also covers your notes outside jarvis/ and Home Assistant's data (a clipped page or a calendar
invite can say "turn off every light"). Once a turn has read any, a change whose arguments the user's own words
don't account for (a name, a room, a number they never said) needs a human too, even when it quotes nothing.
"Turn off the study lamp" after a look at the house is the user's own request and runs; "all lights" after
reading a clipping nobody asked to act on is held. Voice stays usable and the page can't pick the target."""

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
# any form of the word: "I don't see it saved in notes" asks for the write as much as "save it" (it was refused)
ASKS_TO_SAVE = re.compile(r"\b(?:sav(?:e[ds]?|ing)|notes?|noted|jot(?:ted)?|journal(?:ed)?|log(?:ged)?|add(?:ed)?|put|"
                          r"writ(?:e|ten|ing)|remember(?:ed)?|record(?:ed)?|keep|kept|stor(?:e[ds]?|ing)|track(?:ed)?|"
                          r"update[ds]?)\b", re.I)
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


def _root(w: str) -> str:
    """The word without its ending, so "worked" is the "work" the user said and "companies" their "companys"."""
    return re.sub(r"(?:ies|ys|ing|ed|es|s|ly)$", "", w) if len(w) > 4 else w


def own_words(tool, args, t) -> Verdict | None:
    if tool.name not in NOTE_TOOLS:
        return None
    said = getattr(t, "said", "") or t.heard
    if tool.name in NOTE_WRITES and not ASKS_TO_SAVE.search(said):
        return Verdict("error", "ERROR: the user didn't ask you to write anything down. Answer them without it.")
    text = " ".join(str(v) for k, v in args.items() if k in ("text", "fact") and isinstance(v, str))
    words = [_root(w) for w in _WORDS.findall(text.lower()) if len(w) > 3 and w not in _PLAIN]
    known = {_root(w) for w in _WORDS.findall((t.heard + " " + " ".join(t.seen)).lower())}
    new = [w for w in words if w not in known]
    # a few new words are a fair rewording ("used to work" -> "formerly worked"); most of it new is made up
    if words and len(new) > max(3, len(words) // 3):
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


_ARG_FILLER = set("""a an the and or of to in on at for by with from my me i it its this that these those all
every any some please now then off up down out over into is are be set turn switch""".split())


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def ungrounded(tool, args: dict, heard: str) -> list[str]:
    """Words and numbers in a change's arguments the user never said ("all lights" when they asked what a page
    says). Values a schema lists (enum: media keys, on/off) are choices, not text, and are skipped."""
    said = {_stem(w) for w in _WORDS.findall(heard.lower())}
    said |= {d for w, d in _NUMBER_WORDS.items() if w in said}
    props = (tool.parameters or {}).get("properties", {})
    out: list[str] = []

    def look(key, value):
        spec = props.get(key, {})
        if isinstance(value, bool) or value is None or "enum" in spec or "enum" in (spec.get("items") or {}):
            return
        if isinstance(value, list | tuple):
            for v in value:
                look(key, v)
            return
        if isinstance(value, int | float):
            value = str(int(value)) if float(value).is_integer() else str(value)
        if isinstance(value, str):
            for w in _WORDS.findall(value.lower().replace("_", " ")):
                if w not in _ARG_FILLER and _stem(w) not in said and w.lstrip("0") not in said:
                    out.append(w)
    for k, v in args.items():
        look(k, v)
    return list(dict.fromkeys(out))


def after_outside_text(tool, args, t) -> Verdict | None:
    """A change made after this turn read outside text (a note of yours, Home Assistant's data, a web page, a
    message), with something in its arguments the user didn't say: a human decides. JARVIS's own notes and
    journal and a notification to you stay free."""
    read = getattr(t, "read_outside", None)
    # changes only: a search or a question to Claude (external) already asks under the default policy, and the
    # taint check above holds one that would carry the outside text off this machine
    if not read or tool.risk not in (Risk.WRITE, Risk.DESTRUCTIVE) or tool.self_scoped or tool.name == "notify":
        return None
    if not (extra := ungrounded(tool, args, getattr(t, "heard", "") or getattr(t, "said", ""))):
        return None  # everything it acts on, the user said: their request, not the page's
    what = ", ".join(dict.fromkeys(read))
    return Verdict("ask", f'"{" ".join(extra[:4])}" wasn\'t in your request, and this comes after reading '
                          f"{what[:100]}, which may carry instructions that aren't yours")


NOTE_LINE = re.compile(r"^\[([^\]\n]+)\] .*$", re.M)  # kb_search: "[page] text"
TASK_PAGE = re.compile(r" \[([^\]\n]+)\]$", re.M)  # kb_tasks: "TODO text [page]"
PAGE_HEAD = re.compile(r"^# (.+)$", re.M)  # kb_read_page: "# page"
SOURCE_LABEL = {"web_search": "a web search", "fetch_page": "a web page", "adb_notifications": "phone notifications",
                "adb_sms_read": "text messages", "adb_call_log": "the call log", "fs_read": "a file",
                "fs_list": "a folder listing", "win_ps_read": "PowerShell output", "win_ps_run": "PowerShell output"}


def _outside(page: str) -> bool:
    return not page.strip().lower().startswith("jarvis/")


def _note_text(tool, output: str) -> tuple[list[str], str]:
    """(pages outside jarvis/ this note read showed, their text) for the note tools; ([], "") otherwise."""
    if tool.name == "kb_search":
        lines = [(m[1], m[0]) for m in NOTE_LINE.finditer(output) if _outside(m[1])]
        return [p for p, _ in lines], "\n".join(text for _, text in lines)
    if tool.name == "kb_read_page" and (m := PAGE_HEAD.search(output)) and _outside(m[1]):
        return [m[1].strip()], output
    if tool.name == "kb_tasks":
        pages = [p for p in TASK_PAGE.findall(output) if _outside(p)]
        return pages, output if pages else ""
    return [], ""


def record_taint(tool, args, output: str, t) -> str:
    read = getattr(t, "read_outside", None)
    pages, text = _note_text(tool, output)
    if pages:
        t.tainted.append(text)
        if read is not None:
            read.extend(f"your note {p}" for p in dict.fromkeys(pages))
    elif tool.name in TAINT_SOURCES or "__" in tool.name and tool.risk is Risk.READ and len(output) > 200:  # MCP reads
        t.tainted.append(output)
        if read is not None:
            read.append(SOURCE_LABEL.get(tool.name, "Home Assistant's data" if tool.origin == "homeassistant"
                                         else tool.model_name))
    return output


PRE_TOOL: list[Callable] = [arguments, needless_search, needless_time, clock_time, keep_links, own_words, slots, taint,
                            after_outside_text]
POST_TOOL: list[Callable] = [record_taint]


def audit_text(tool, output: str) -> str:
    """What the audit log keeps of a result: personal message text stays out of it."""
    if tool.name in REDACT_IN_AUDIT and not output.startswith("ERROR"):
        return f"({len(output)} characters, not kept in the audit)"
    return output[:2000]
