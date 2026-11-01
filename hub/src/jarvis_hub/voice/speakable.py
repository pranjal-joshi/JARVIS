"""Turn an on-screen reply (possibly markdown) into something worth saying aloud.

The HUD shows the full reply; speech gets the prose, without markup, code or tables.
Replies are meant to be short already (the persona caps them); the sentence cap here
is only a quiet safety net for a runaway answer."""

import re

MAX_SPOKEN = 500

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_SIGNATURE = re.compile(r"^\s*— .*$", re.M)  # ask_claude/ask_opencode footer: "— claude · model (9 s)"
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKS = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),           # headings
    (re.compile(r"^\s*>\s?", re.M), ""),                     # quotes
    (re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M), ""),     # list markers
    (re.compile(r"`([^`]*)`"), r"\1"),                       # inline code
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),                # bold
    (re.compile(r"(?<![\w*])[*_](\S.*?)[*_](?![\w*])"), r"\1"),  # italics
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"^\s*([-*_]\s*){3,}$", re.M), ""),          # rules
]
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def to_speech(text: str, max_chars: int = MAX_SPOKEN) -> str:
    """Spoken form of a reply; "" when nothing in it is worth saying (e.g. only code)."""
    out = _FENCE.sub(" ", text)
    out = _TABLE_ROW.sub("", out)
    out = _SIGNATURE.sub("", out)
    out = _LINK.sub(r"\1", out)
    for rx, rep in _MARKS:
        out = rx.sub(rep, out)
    # each remaining line reads as its own sentence
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    out = " ".join(ln if ln[-1] in ".!?:;," else ln + "." for ln in lines)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > max_chars:
        spoken = ""
        for s in _SENTENCE.split(out):
            if spoken and len(spoken) + len(s) > max_chars:
                break
            spoken = f"{spoken} {s}".strip()
        out = spoken[:max_chars]
    if out.endswith(":"):
        out = out[:-1] + "."
    return out
