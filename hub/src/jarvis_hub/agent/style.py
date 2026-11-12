"""Reply style enforced in code, not in the prompt.

A 2B model ignores most style rules (bullets, bold, "Would you like me to...", long answers), and
every rule in the prompt costs tokens on every turn. So the persona keeps three style lines and this
module does the rest, sentence by sentence, the same way for a streamed reply (SentenceGate) and a
whole one (tidy). Replies with a code block are left as written on the HUD."""

import re
from collections.abc import Callable

from jarvis_hub.config import Source

MAX_SENTENCES = {Source.SATELLITE: 2, Source.PHONE: 2, Source.WEB: 4}
_BOUNDARY = re.compile(r"(?<=[.!?…])[\"')\]]?\s+|\n+")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s*")
_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1|(?<![\w*])\*(\S[^*]*?)\*(?![\w*])")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️]")
# offers and padding a small model adds to nearly every answer
_OFFER = re.compile(
    r"^(?:sir,?\s+)?(?:would you like|do you want me to|shall i (?:also|look|check|find|dig|get|go on)|"
    r"let me know|feel free|if you(?:'d| would)? like|is there anything else|anything else|i can also|"
    r"happy to help|hope (?:this|that) helps|just say the word|should you need)", re.I)
_SIGNOFF = re.compile(r"^(?:—|-)?\s*(?:J\.?A\.?R\.?V\.?I\.?S\.?|your (?:faithful )?assistant)\W*$", re.I)
_SIR_MID = re.compile(r",\s*sir(?=[\s.,;:?!…]|$)")  # "Very good, sir." "Done, sir: ..." (lower case: not "Sir Walter")
_SIR_LEAD = re.compile(r"(^\W*|[.!?…]\s+)Sir,\s*")


def limit(source: Source, wit: int = 1) -> int:
    """Sentences a reply may keep: one more at the playful wit level (persona/wit.py), room for the quip."""
    return MAX_SENTENCES[source] + (wit >= 2)


def address(text: str, word: str = "sir") -> str:
    """Swap the hub's own "sir" for the configured form of address ("" drops it). The model is told the same."""
    if word == "sir" or "sir" not in text.lower():
        return text
    text = _SIR_MID.sub(f", {word}" if word else "", text)
    return _SIR_LEAD.sub(lambda m: m[1] + (f"{word[:1].upper()}{word[1:]}, " if word else ""), text)


def _clean(sentence: str) -> str:
    s = _HEADING.sub("", _BULLET.sub("", sentence))
    s = _EMPHASIS.sub(lambda m: m[2] or m[3] or "", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", _EMOJI.sub("", s)).strip()
    if s.endswith(":"):  # "Here are the highlights:" with the list gone
        s = s[:-1] + "."
    elif s and s[-1] not in ".!?…\"')]":
        s += "."
    return s


def _keep(sentence: str, kept: int) -> bool:
    if not sentence or _SIGNOFF.match(sentence):
        return False
    return kept == 0 or not _OFFER.match(sentence)  # an offer alone is still an answer


def split(text: str) -> tuple[list[str], str]:
    """Complete sentences (and list items) in text, and the unfinished rest."""
    parts, last = [], 0
    for m in _BOUNDARY.finditer(text):
        parts.append(text[last:m.start()].strip())
        last = m.end()
    return [p for p in parts if p], text[last:]


def tidy(text: str, source: Source, wit: int = 1) -> str:
    if "```" in text:  # code on the HUD stays exactly as written
        return text.strip()
    done, rest = split(text)
    out: list[str] = []
    for s in [*done, rest]:
        s = _clean(s)
        if len(out) < limit(source, wit) and _keep(s, len(out)):
            out.append(s)
    return " ".join(out)


class SentenceGate:
    """Wraps a stream sink: passes on whole, cleaned sentences, drops offers, stops at the cap."""

    def __init__(self, emit: Callable[[str], None], source: Source, wit: int = 1, word: str = "sir") -> None:
        self.emit, self.source, self.max, self.word = emit, source, limit(source, wit), word
        self.buf, self.kept, self.raw = "", 0, False

    def feed(self, piece: str) -> None:
        if self.raw:
            return self.emit(piece)
        self.buf += piece
        if "```" in self.buf:  # code from here on: pass it through untouched
            self.raw = True
            self.emit(self.buf)
            self.buf = ""
            return
        done, self.buf = split(self.buf)
        for s in done:
            self._say(s)

    def flush(self) -> None:
        if not self.raw and self.buf.strip():
            self._say(self.buf)
        self.buf = ""

    def reset(self) -> None:
        self.buf, self.kept, self.raw = "", 0, False

    def _say(self, sentence: str) -> None:
        s = address(_clean(sentence), self.word)
        if self.kept < self.max and _keep(s, self.kept):
            self.emit(("" if self.kept == 0 else " ") + s)
            self.kept += 1
