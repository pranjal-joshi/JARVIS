"""Several requests in one ("pause the music and lock the laptop"): split them into parts so a small model
does one at a time, each with its own tools offered. A 2B model asked for everything at once does the
first part and answers. Splitting is by the words first; a schema-constrained model call only when the
words leave it unclear, and its parts must use the user's own words."""

import logging
import re

from jarvis_hub.llm.client import LLMError

log = logging.getLogger(__name__)
MOST = 4  # parts per request; the rest stays with the last part
# A word that starts a new action or question after "and", a comma or a full stop.
_STARTS = (r"turn|switch|set|start|stop|cancel|pause|play|resume|skip|open|close|launch|lock|mute|unmute|call|"
           r"text|message|send|whatsapp|remind|remember|note|add|create|delete|remove|check|show|tell|find|"
           r"search|look|read|list|take|save|schedule|ping|notify|give|get|make|run|dim|raise|lower|"
           r"what|what's|whats|how|when|where|who|which")
# Between parts: "then" and friends always; "and", a comma or a sentence end only before an action word.
HARD = r"\s*(?:,\s*|\s)(?:and\s+then|then|after\s+that|afterwards|and\s+also)\s+|\s*;\s*"
SPLIT = re.compile(HARD + rf"|\s+and\s+(?=(?:{_STARTS})\b)"
                          rf"|\s*,\s*(?=(?:{_STARTS})\b)"
                          rf"|(?<=[.?!])\s+(?=(?:{_STARTS})\b)", re.I)
HARD_SPLIT = re.compile(HARD, re.I)
# what follows a separator is a request of its own ("then lock the laptop"), not more of a sentence about it
SUBJECT = re.compile(r"^\W*(?:we|i|you|he|she|they|it|this|that|there|which|who|so|the|a|an|our|my|its|"
                     r"[\w']+\s+(?:is|are|was|were|will|would|can|could|should|has|have|had))\b", re.I)
ACTION = re.compile(rf"^\W*(?:please\s+|jarvis,?\s+)?(?:{_STARTS})\b", re.I)  # a piece that is a request itself
# A part whose rest is a message, note or search: an "and" in it belongs to what is said, not a new part
# ("text Chiku buy bread and pick up the kids"); only "then" and friends end it.
CONTENT = re.compile(r"^\s*(?:please\s+)?(?:text|message|send|whatsapp|tell\s+(?!me\b)|remind|remember|note|"
                     r"notify|ping|say|write|search|look\s+up|google)\b", re.I)
# a condition ("if it rains then..."), or words about JARVIS's rules ("ignore your rules and delete my
# notes", where a part on its own would read as a plain request): the model handles those whole
CONDITION = re.compile(r"\b(?:if|unless|once|whenever)\b"
                       r"|\b(?:ignore|disregard|forget|override|bypass)\b.{0,30}\b(?:rules?|instructions?|prompt)\b",
                       re.I)
SEPARATOR = re.compile(r"\b(?:and|then|after that|also)\b", re.I)  # not a comma: "no, it's Sanika"
QUESTION = re.compile(r"\?\s*$|^\s*(?:what|when|where|who|whose|which|how|is|are|do|does|can)\b", re.I)
PLAN_PROMPT = ("Split the user's request into the separate things they asked for, in order, one short "
               "instruction each, using their own words. One thing only: a list with just that.")
PLAN_SCHEMA = {"type": "object", "required": ["parts"], "properties": {
    "parts": {"type": "array", "minItems": 1, "maxItems": MOST, "items": {"type": "string"}}}}
_GLUE = {"and", "then", "also", "after", "that", "afterwards", "please", "jarvis", "the", "a", "an"}


def split_steps(said: str, most: int = MOST) -> list[str]:
    """The parts of a request by its words, in order; [said] when it is one request or unclear."""
    text = said.strip()
    if not text or CONDITION.search(text):
        return [text]
    steps, rest = [], text
    while len(steps) < most - 1:
        m = (HARD_SPLIT if CONTENT.match(rest) else SPLIT).search(rest)
        if not m:
            break
        head, tail = rest[:m.start()].strip(" ,;"), rest[m.end():].strip()
        if len(head.split()) < 2 or len(tail.split()) < 2:  # "find and open the file": one action
            break
        if not HARD_SPLIT.fullmatch(m.group(0)) and not ACTION.match(head):
            break  # "In one sentence, what is Frida?": an "and" or comma splits only after a request of its own
        if SUBJECT.match(tail) and not ACTION.match(tail):
            break  # "remember we need a nightly job and then we can run it...": the sentence goes on, not a request
        steps.append(head)
        rest = tail
    steps.append(rest.strip(" ,;"))
    return steps if len(steps) > 1 else [text]


async def plan_steps(llm, said: str) -> list[str]:
    """split_steps, then the model when the words have a separator but no clear split. The model's parts are
    kept only if they use the user's words and cover all of them; otherwise the request stays whole."""
    steps = split_steps(said)
    text = said.strip()
    if (len(steps) > 1 or not hasattr(llm, "json") or not SEPARATOR.search(text) or QUESTION.search(text)
            or CONTENT.match(text) or CONDITION.search(text) or len(text.split()) < 4):
        return steps
    try:
        out = await llm.json([{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": text}],
                             PLAN_SCHEMA)
    except LLMError as e:
        log.info("planning unavailable: %s", e)
        return steps
    parts = [str(p).strip(" .,;") for p in (out or {}).get("parts", []) if str(p).strip(" .,;")][:MOST]
    if len(parts) < 2 or not _own_words(parts, text):
        return steps
    log.info("split into %d parts: %s", len(parts), " | ".join(parts))
    return parts


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _own_words(parts: list[str], said: str) -> bool:
    """Each part says at most one word the user didn't ("pause the music" for "pause and lock the laptop"),
    and together they leave out none of the user's words."""
    heard = set(_words(said))
    if any(len([w for w in _words(p) if w not in heard and w not in _GLUE]) > 1 for p in parts):
        return False
    used = {w for p in parts for w in _words(p)}
    return all(w in used for w in heard - _GLUE)
