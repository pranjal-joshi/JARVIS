"""Symbols said as words, written as symbols: "add it to work slash AI Studio" -> "work/AI Studio", "rea dot tools"
-> "rea.tools", "h t t p s colon slash slash" -> "https://". Speech-to-text writes what it hears, so a page name,
a link or a path said aloud reaches the model as words and lands in the wrong place.

Only between two words (or symbols), so a word on its own stays a word: "play Slash" is the guitarist, "a dash
of salt" stays as it is unless it joins two words with no "of"."""

import re

# (spoken, symbol, joins: no spaces around it)
_SYMBOLS = (
    (r"back ?slash", "\\"),
    (r"(?:forward )?slash", "/"),
    (r"underscore", "_"),
    (r"(?:hyphen|dash)", "-"),
    (r"dot", "."),
    (r"at (?:sign|the rate)", "@"),
)
# between two pieces: "work slash AI", "https: slash www", "a slash b"
_BETWEEN = [(re.compile(rf"(?<=\S)\s+{w}\s+(?=[^\s.,!?])", re.I), s) for w, s in _SYMBOLS]
_SCHEME = re.compile(r"(?<=\w)\s+colon\s+(?:forward\s+)?slash\s+(?:forward\s+)?slash\s+", re.I)  # "https://"
_COLON = re.compile(r"(?<=\w)\s+colon(?:\s+(?=/)|\b)", re.I)  # "https colon slash slash" -> "https://"
_HASH = re.compile(r"\bhash ?tag\s+(?=\w)|\bhash\s+(?=\w)", re.I)  # "hashtag work" -> "#work"
_NOT_JOINING = re.compile(r"\b(?:of|a|the|to|and|in|on|for)\s+(?:hyphen|dash)\b", re.I)  # "a dash of salt"


def symbols(text: str) -> str:
    """Spoken symbols as characters; text with none is returned as it was."""
    if not text or not re.search(r"slash|underscore|hyphen|dash|dot|colon|hash|at sign|at the rate", text, re.I):
        return text
    out = _SCHEME.sub("://", text)
    out = _COLON.sub(":", out)
    for _ in range(4):  # "slash slash": each pass joins one more
        before = out
        for rx, sym in _BETWEEN:
            if sym == "-" and _NOT_JOINING.search(out):
                continue
            out = rx.sub(sym.replace("\\", "\\\\"), out)
        if out == before:
            break
    return _HASH.sub("#", out)
