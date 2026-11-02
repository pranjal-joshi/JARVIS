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

# tools whose output is someone else's words
TAINT_SOURCES = {"web_search", "fetch_page", "adb_notifications", "adb_sms_read", "adb_call_log", "fs_read",
                 "fs_list"}
# reads whose full text shouldn't sit in the audit log (it keeps a short note instead)
REDACT_IN_AUDIT = {"adb_notifications", "adb_sms_read", "adb_call_log", "fs_read"}

# What makes a web search worth it: the user asked for one, or wants something current.
CURRENT = re.compile(
    r"\b(search|look (?:it |that )?up|google|web|online|internet|latest|news|today|tonight|yesterday|tomorrow|"
    r"current(?:ly)?|now|recent(?:ly)?|this (?:week|month|year|season)|price|cost|rate|score|stock|"
    r"open|hours|release[ds]?|update[ds]?|live|20\d\d|who won|result)\b", re.I)

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
SPAN = 5  # this many words in a row from outside text = carried over


@dataclass
class Verdict:
    kind: str  # "error" | "ask"
    message: str


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
        bad += [n for n in re.findall(r"\d+", value) if n not in digits and n.lstrip("0") not in digits]
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
def needless_search(tool, args, t) -> Verdict | None:
    if tool.name == "web_search" and not CURRENT.search(t.heard):
        # a 2B model searches for "the capital of Australia": a credit, an approval and 20 s, for nothing
        return Verdict("error", "ERROR: that is general knowledge, not current news. Answer it yourself without "
                                "searching; if you really don't know, say so.")
    return None


def slots(tool, args, t) -> Verdict | None:
    if bad := unsaid(tool, args, t.heard, t.seen):
        return Verdict("error", f"ERROR: {tool.model_name} has {', '.join(bad)}, which the user didn't say. "
                                "Use the user's own words, names, dates and numbers, then call it again.")
    return None


def taint(tool, args, t) -> Verdict | None:
    if tool.risk is not Risk.READ and (span := carried(args, t.tainted, t.heard)):
        return Verdict("ask", f'carries outside text ("{span}…") from a web page, message or file')
    return None


def record_taint(tool, args, output: str, t) -> str:
    if tool.name in TAINT_SOURCES or "__" in tool.name and tool.risk is Risk.READ and len(output) > 200:  # MCP reads
        t.tainted.append(output)
    return output


PRE_TOOL: list[Callable] = [needless_search, slots, taint]
POST_TOOL: list[Callable] = [record_taint]


def audit_text(tool, output: str) -> str:
    """What the audit log keeps of a result: personal message text stays out of it."""
    if tool.name in REDACT_IN_AUDIT and not output.startswith("ERROR"):
        return f"({len(output)} characters, not kept in the audit)"
    return output[:2000]
