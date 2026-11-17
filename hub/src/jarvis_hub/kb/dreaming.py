"""Dreaming: once a night JARVIS goes through the last days' journal, the memories it saved and what the user
said to it, and keeps what is worth keeping on jarvis/dreams. The short version of that page rides in every
conversation, so what matters is known without a search.

The shape follows three systems that do this:
- OpenClaw (memory-core dreaming): phases light -> REM -> deep. Light gathers and dedupes recent material, REM
  finds the themes that recur, deep scores candidates and is the only phase that writes durable memory. The
  scoring weights are OpenClaw's (relevance .30, frequency .24, diversity .15, recency .15, consolidation .10,
  richness .06). The model returns add / merge / supersede decisions that cite their candidates, never free
  text; a decision that cites nothing, or says a name or number its sources don't, is dropped; if the model
  fails, the best candidates are added as they were said (append-only). A diary records each night.
- Hermes (hermes-dreaming): a wake gate (a quiet night calls no model), corroboration (the user's own words
  count; JARVIS's own notes alone count less), at most 6 changes a night of which 3 new, the same fact with
  different numbers or names is a question for the user rather than a silent update, and secrets and
  instruction-like text never become memory.
- Claude Code (auto dream): runs after enough time and new activity, consolidates (merges duplicates, makes
  relative dates absolute, replaces what was contradicted), a lock so two runs can't overlap, and a short
  index loaded into every session.

What is kept are the facts on the memory pages (jarvis/people, family, work...): a dream adds to them, merges
duplicates, replaces what was corrected and moves what is misfiled, each change undoable from the morning digest.
There is no second copy: what rides in every conversation is read from those pages (context()), so a correction is
never out of date there. Disagreements become questions, asked once and answered in the digest. Each night's
report goes to jarvis/dreams/diary. (Before 14 Nov 2026 a separate jarvis/dreams page held the kept facts.)"""

import asyncio
import copy
import hashlib
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from jarvis_hub.approval.gate import Risk
from jarvis_hub.kb.graph import FRONTMATTER, JARVIS_NS, JOURNAL_FILE, LINK, PROP, ObsidianVault, journal_title
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry
from jarvis_hub.files import write_atomic

log = logging.getLogger(__name__)

PAGE = "jarvis/dreams"
DIARY = "jarvis/dreams/diary"
INBOX = "jarvis/inbox"  # quick captures (the capture tool): trusted candidates, cleared once a dream went through them
ENTITIES = "jarvis/entities/"  # one page per named thing: type, aliases and relations in its frontmatter
ENTITY_TYPES = ("person", "project", "place", "device", "org", "concept", "other")
SINGLE = ("located_at", "lives_in", "works_at", "born_on")  # relations with one target: two is a question
STALE_DAYS = 180  # a volatile fact this old is asked about ("Is this still true?"), never twice in this time
MAX_STALE = 2  # such questions a night
VOLATILE = ("jarvis/location", "jarvis/home", "jarvis/work", "jarvis/tools")  # and entities of type device/place
DIARY_DAYS = 30  # diary entries kept; older days roll off (jarvis/dreams keeps what mattered)
WEIGHTS = {"relevance": 0.30, "frequency": 0.24, "diversity": 0.15, "recency": 0.15, "consolidation": 0.10,
           "richness": 0.06}
MIN_SCORE = 0.35  # a candidate below this isn't put to the model
MAX_CANDIDATES = 12
MAX_CHANGES, MAX_NEW = 6, 3  # per night (Hermes)
MAX_ENTRIES = 80  # shortcut: the model sees at most this many kept facts a night; page through them past that
RATE_WAIT_S = 20  # a dream model rate limited for this long or less is waited for once, not swapped out
MAX_QUESTIONS = 50 # questions remembered (asked, answered, dismissed) so none is asked twice
CONTEXT_CHARS = 1300  # what rides in every conversation (about 300 tokens): enough for every CORE page
# who the user is: these pages ride in every conversation first, in this order; the newest of the rest after
CORE = ("jarvis/people", "jarvis/family", "jarvis/location", "jarvis/home", "jarvis/work")
HALF_LIFE_DAYS = 7
SAME = 0.6  # share of words two texts must have in common to be the same thing
TRUSTED_VIA = ("said by you", "corrected by you", "edited by you", "answered by you")
SECRET = re.compile(r"\b(?:password|passcode|pin|otp|api[ _-]?key|token|secret|cvv|card number)\b\s*(?:is|:|=)", re.I)
INSTRUCTION = re.compile(r"\b(?:ignore (?:all |the )?(?:previous|above)|system prompt|you are now|disregard)\b", re.I)
STOP = set("""a an the and or but if then so to of in on at by for from with about as is are was were be been being
it its this that these those i me my mine you your yours we our us he him his she her they them their what which who
whom when where why how do does did done have has had will would shall should can could may might must not no yes
just also very really than too only up down out over again some any all each every there here today yesterday
tomorrow now sir jarvis user users learned see noted corrected says said tell told ask asked please thanks ok okay""".split())
_ME = re.compile(r"\b(?:i|i'm|i've|my|me|mine|the user|user's|wife|husband|son|daughter|mother|mom|mum|father|dad|"
                 r"brother|sister|family|friend|birthday|anniversary|prefer|like|love|hate|allergic|live|work|job|"
                 r"name|called|always|never|usually|project|plan|goal)\b", re.I)
COMMAND = re.compile(r"(?i)^\W*(?:(?:hey |ok |okay |please |jarvis\W+)*)(?:remember to (?:remind|tell|ask|set|send|"
                     r"call|text)|(?:the user )?(?:asked|requested|wants?) (?:a |me |you |jarvis )?(?:to )?remind\w*|what|who|when|where|how|why|can|could|"
                     r"would|will|is|are|do|does|did|run|turn|switch|set|open|play|make|create|delete|remove|remind|"
                     r"send|text|message|call|search|look|find|show|tell me|read|check|ask|add|put|cancel|stop|start|"
                     r"move|schedule|give me|list)\b")
_DECORATION = re.compile(r"\s*\(see \[\[[^\]]+\]\]\)|\s*#jarvis\b|^(?:Learned|Corrected|Noted|Today):\s*", re.I)

DEEP_PROMPT = """You tidy a home assistant's memory of its user. The memory is kept on topic pages: people, family,
work, home, location, dates, preferences, projects, tools, terms, memory. You get the facts kept now (E1, E2...,
each with its page) and new candidates from the last days that aren't kept yet (C1, C2...). Decide:
- add: a candidate worth keeping that no fact says yet. text: one plain sentence; topic: its page.
- merge: facts and/or candidates that say the same thing: one sentence replaces every one cited.
- supersede: a candidate that corrects or updates a fact: entry: that fact (E3); text: the corrected sentence.
- move: a fact on the wrong page (a relative on work, a job on family): entry: that fact; topic: the right page.
- entity: a named thing (a person, project, device, place) with 2 or more facts gets its own page: cites: those
  facts (E ids); entry: a short slug ("z2m"); text: its name ("Z2M"); type: person, project, place, device, org,
  concept or other; aliases: its other names and abbreviations as the facts write them ("Zigbee2MQTT");
  relations: "part_of homelab", "located_at new-home" (a relation, then another entity's slug). Facts already on
  its page (entities/...) stay there.
- skip: chit-chat, one-off questions, things only true for a moment, guesses, and requests to the assistant
  (reminders, timers, messages, "remember to remind me..."): those are handled elsewhere, not memories.
Keep what will still matter in a month: people and their names, dates, preferences, plans, projects, places.
Write each text as a fact about the user ("The user's wife is Sanika."), with full dates that include the year
("on 3 November 2026"; never "today", "tomorrow" or a date without its year: each candidate shows the day it
was said). Birthdays and anniversaries recur every year: give them without a year ("born on 13 May"). Use only names, numbers and dates that are in the cited candidates or entry; never add any.
Every decision cites the facts and candidates it rests on (E and C ids). Change only what is duplicated, wrong,
out of date or misfiled; leave a good fact alone. themes: one or two sentences on what the user has been
occupied with. conflicts: pairs of ids (E or C) that cannot both be true: two names for one person, two dates
for one birthday, two cities for one home. Facts that follow each other (an old job and a new one, a plan and its
delay) or only say more are not conflicts. questions: only something unclear you can't settle from the facts, as a
short question to the user; usually none."""
DEEP_SCHEMA = {"type": "object", "properties": {
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["add", "merge", "supersede", "move", "entity", "skip"]},
        "cites": {"type": "array", "items": {"type": "string"}},
        "entry": {"type": "string"},
        "topic": {"type": "string"},
        "text": {"type": "string"},
        "type": {"type": "string", "enum": list(ENTITY_TYPES)},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "relations": {"type": "array", "items": {"type": "string"}}}, "required": ["op", "cites"]}},
    "conflicts": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
    "themes": {"type": "string"},
    "questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["decisions"]}


# ---------- light: gather ----------
@dataclass
class Bit:
    """One thing said or noted: a journal line, a saved memory, or the user's words to JARVIS."""
    text: str
    day: date
    kind: str  # journal | memory | said
    trusted: bool  # the user's own words (not something JARVIS concluded or read)
    where: str


@dataclass
class Candidate:
    bits: list[Bit]
    score: float = 0.0
    signals: dict = field(default_factory=dict)

    @property
    def text(self) -> str:  # the clearest wording: the user's own, then the longest
        return max(self.bits, key=lambda b: (b.trusted, b.kind != "said", len(b.text))).text


def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9']+", text.lower()):
        w = w.strip("'")
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) > 2 and w not in STOP:
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
        elif w.isdigit():
            out.add(w)
    return out


def similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _clean(text: str) -> str:
    text = LINK.sub(lambda m: m[1], _DECORATION.sub("", text.split("\n")[0])).strip()
    return re.sub(r"\s+", " ", text)


def _day_of(name: str) -> date | None:
    """The day a journal page name stands for: "2026-11-05" (older notes: "Nov 5th, 2026")."""
    for text, fmt in ((name.strip(), "%Y-%m-%d"), (re.sub(r"(\d+)(st|nd|rd|th)", r"\1", name), "%b %d, %Y")):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def gather(graph: ObsidianVault, traces, since: date, until: date) -> list[Bit]:
    """Journal lines and saved memories from the days since..until, and the user's own statements to JARVIS."""
    bits: list[Bit] = []
    for page in graph.pages():
        low = page.name.lower()
        if low == PAGE or low.startswith(PAGE + "/"):
            continue  # what dreaming wrote is never its own source
        if low == INBOX:  # captured on purpose, whenever: the user's own words (the first line, as on the page)
            for b in page.blocks:
                props = {m[1].lower(): m[2] for line in b.text.splitlines()[1:] if (m := PROP.match(line))}
                learned = _learned(props)
                bits.append(Bit(b.text.splitlines()[0].strip(), until if learned == date.min else learned,
                                "said", True, INBOX))
        elif page.is_journal:
            m = JOURNAL_FILE.match(page.path.name)
            day = date(int(m[1]), int(m[2]), int(m[3])) if m else None
            if not day or not since <= day <= until:
                continue
            for b in page.blocks:
                ours = "#jarvis" in b.text.lower() or "jarvis" in b.links
                if (text := _clean(b.text)) and not COMMAND.match(text):
                    bits.append(Bit(text, day, "journal", not ours, page.name))
        # the memory pages aren't candidates: they are what is kept (E1, E2...), tidied in place
    for said, ts in _said(traces, since, until):
        bits.append(Bit(said, datetime.fromtimestamp(ts).date(), "said", True, "conversation"))
    return [b for b in bits if not SECRET.search(b.text) and not INSTRUCTION.search(b.text) and len(b.text) >= 8]


def _said(traces, since: date, until: date) -> list[tuple[str, float]]:
    """The user's own words to JARVIS: no skill runs, evals or automations, no questions, no commands."""
    if traces is None:
        return []
    start = datetime.combine(since, datetime.min.time()).timestamp()
    end = datetime.combine(until + timedelta(days=1), datetime.min.time()).timestamp()
    try:
        with traces._lock:
            rows = traces._conn.execute("SELECT said, ts, session, data FROM turns WHERE ts >= ? AND ts < ? "
                                        "ORDER BY ts", (start, end)).fetchall()
    except Exception:
        log.exception("dreaming couldn't read the traces")
        return []
    out = []
    for said, ts, session, data in rows:
        said = (said or "").strip()
        if not said or session.startswith(("skill-", "eval")) or said.startswith(("(Automation", "(system")):
            continue
        try:
            meta = json.loads(data)
        except ValueError:
            meta = {}
        if meta.get("dry_run") or meta.get("tainted"):
            continue
        # statements about the user ("my wife's birthday is 20 Aug"), not questions or commands
        if said.endswith("?") or not _ME.search(said) or COMMAND.match(said):
            continue
        out.append((said[:300], ts))
    return out


# ---------- light: dedupe into candidates; REM: themes ----------
def differ(a: str, b: str) -> bool:
    """Each names someone or a number the other doesn't: "son Chiku's birthday is 13 May" / "Aarav's birthday"."""
    ma, mb = _marks(a), _marks(b)
    return bool(ma - mb) and bool(mb - ma)


def cluster(bits: list[Bit]) -> list[Candidate]:
    cands: list[tuple[set[str], Candidate]] = []
    for b in bits:
        w = words(b.text)
        if len(w) < 2:
            continue
        for cw, c in cands:
            if similar(w, cw) >= SAME and not any(differ(b.text, o.text) for o in c.bits):
                c.bits.append(b)
                cw |= w
                break
        else:
            cands.append((w, Candidate([b])))
    return [c for _, c in cands]


def themes(bits: list[Bit], n: int = 5) -> list[str]:
    """Words that come back on more than one day (REM: what the user keeps returning to)."""
    days: dict[str, set[date]] = {}
    for b in bits:
        for w in words(b.text):
            if not w.isdigit():
                days.setdefault(w, set()).add(b.day)
    ranked = sorted(((len(d), w) for w, d in days.items() if len(d) > 1), reverse=True)
    return [w for _, w in ranked[:n]]


# ---------- deep: score ----------
def score(c: Candidate, today: date, span_days: int) -> float:
    texts = " ".join(b.text for b in c.bits)
    age = min((today - b.day).days for b in c.bits)
    s = {"relevance": min(1.0, 0.4 * bool(_ME.search(texts)) + 0.3 * bool(re.search(r"\b[A-Z][a-z]{2,}", texts))
                          + 0.3 * any(b.trusted for b in c.bits)),
         "frequency": min(len(c.bits), 4) / 4,
         "diversity": len({b.kind for b in c.bits}) / 3,
         "recency": math.exp(-math.log(2) * max(age, 0) / HALF_LIFE_DAYS),
         "consolidation": min(len({b.day for b in c.bits}), 3) / max(1, min(span_days, 3)),
         "richness": min(1.0, 0.5 * bool(re.search(r"\d", texts)) + 0.5 * bool(re.search(r"https?://|\[\[", texts))
                         + 0.3 * bool(re.search(r"\b[A-Z][a-z]{2,}", texts)))}
    c.signals = {k: round(v, 2) for k, v in s.items()}
    total = sum(WEIGHTS[k] * v for k, v in s.items())
    if not any(b.trusted for b in c.bits):
        total *= 0.7  # only JARVIS's own notes say so (Hermes: the user's words corroborate)
    c.score = round(min(total, 1.0), 3)
    return c.score


def _marks(text: str, strict: bool = False) -> set[str]:
    """Names and numbers in a text. strict (what a memory may not add to its sources): every capitalised word;
    otherwise (do two texts name different things) a sentence's first word counts only as "Aarav's ...", since
    "Remember to..." and "Remind the..." name nothing."""
    caps = set()
    for m in re.finditer(r"\b[A-Z][a-zA-Z]{2,}", text):
        first = not text[:m.start()].strip() or text[:m.start()].rstrip()[-1:] in ".!?\"'“(:"
        if strict or not first or text[m.end():m.end() + 2] == "'s":
            caps.add(m[0].lower())
    return (caps - STOP) | set(re.findall(r"\d+", text))


def grounded(text: str, sources: list[str]) -> bool:
    said = " ".join(sources).lower()
    return all(m in said for m in _marks(text, strict=True) - {"user", "the", "jarvis"})


def conflicts(cands: list[Candidate], kept: list[str]) -> list[tuple[str, str]]:
    """The same subject with different names or numbers (Hermes): a question, not a silent update."""
    texts = list(dict.fromkeys([b.text for c in cands for b in c.bits] + kept))
    out = []
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            wa, wb = words(a), words(b)
            ma, mb = _marks(a), _marks(b)
            # one or two names or numbers each where the rest agrees ("son Chiku's birthday 13 May" / "Aarav's
            # birthday 13 May"); a text that only says much more is not a contradiction
            if 0.5 <= similar(wa, wb) < 0.95 and len(wa & wb) >= 2 and differ(a, b) and \
                    len(ma - mb) <= 2 and len(mb - ma) <= 2:
                out.append((a, b))
    return out[:4]


# ---------- the service ----------
@dataclass
class Report:
    started: float
    since: str
    until: str
    gathered: int = 0
    candidates: int = 0
    added: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)  # names given a page of their own, their facts moved there
    orphans: list[str] = field(default_factory=list)  # empty jarvis pages nothing links to (noted, not deleted)
    inbox: int = 0  # captures taken off jarvis/inbox
    skipped: int = 0
    questions: list[str] = field(default_factory=list)  # new this night (one already asked isn't asked again)
    themes: str = ""
    model: str = ""
    fallback: bool = False
    dry_run: bool = False
    woke: bool = True  # false: nothing new and the memory pages unchanged, so no model was asked
    seconds: float = 0.0
    error: str = ""
    # each change on its own, for the morning digest's undo:
    # {op: added|merged|updated|moved|entity, text, page, raw, was: [{page, text, raw}], undone}; an entity also
    # has facts (what it put on its page), front (type, aliases, relations) and front_was (None: it made the page)
    changes: list[dict] = field(default_factory=list)


def _raw(text: str, props: dict) -> str:
    """A memory as its page holds it: the fact, then its properties."""
    return text + "".join(f"\n  {k}:: {v}" for k, v in props.items() if k != "page")


def _learned(props: dict) -> date:
    return _day_of(LINK.sub(lambda m: m[1], props.get("learned", "")).strip()) or date.min


def _user(props: dict) -> bool:
    """Typed (no via::) or edited by the user in Obsidian: dreaming never deletes, merges, rewrites or moves it."""
    via = props.get("via", "").lower()
    return not via.strip() or "edited by you" in via


def slug(name: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", name.lower()))


def _items(value: str) -> list[str]:
    """A list property's items ("a, b"); a comma inside [[...]] stays (as Page.listprop reads it)."""
    return [x.strip() for x in re.split(r",(?![^\[]*\]\])", value or "") if x.strip()]


def _union(a: list[str], b: list[str]) -> list[str]:
    seen, out = set(), []
    for x in a + b:
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def _relation(text: str) -> str:
    """"part_of homelab" -> "part_of [[jarvis/entities/homelab]]"; "" when it isn't a relation and a target."""
    m = re.match(r"\s*(\w+)\s+(.+?)\s*$", text)
    target = slug(LINK.sub(lambda x: x[1], m[2]).strip().lower().removeprefix(ENTITIES)) if m else ""
    return f"{m[1].lower()} [[{ENTITIES}{target}]]" if target else ""


def _norm(text: str) -> str:
    """A fact without its voice and filler: "My wife's name is Sanika" and "The user's wife is named Sanika" match."""
    t = re.sub(r"\b(?:the user's|your|my|the user|you|i)\b", "user", text.lower())
    t = re.sub(r"\b(?:the|a|an|is|are|was|named|called|name)\b", " ", re.sub(r"'s\b", "", t))
    return " ".join(re.findall(r"[a-z0-9_]+", t))


def duplicate(a: str, b: str) -> bool:
    return not differ(a, b) and SequenceMatcher(None, _norm(a), _norm(b)).ratio() >= 0.9


class Dreamer:
    def __init__(self, graph: ObsidianVault, traces, llm, state_file: Path, at: str = "03:00", days: int = 3,
                 bus=None, on_change=None, model: str = "") -> None:
        self.graph, self.traces, self.llm, self.state_file = graph, traces, llm, state_file
        self.model = model  # the model a dream asks ("" = the chat model of the moment)
        self.at, self.days, self.bus, self.on_change = at, max(1, days), bus, on_change
        self.state = self._load()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        write_atomic(self.state_file, json.dumps(self.state, indent=1, default=str), encoding="utf-8")

    @property
    def running(self) -> bool:
        return self._lock.locked()

    # what is kept: the facts on the memory pages, [(text, props with "page")]
    # (entity pages are jarvis/entities/*: kept as well; the inbox is candidates, not yet kept)
    def kept(self, inbox: bool = False) -> list[tuple[str, dict]]:
        out = []
        for p in self.graph.pages():
            low = p.name.lower()
            if p.is_journal or not low.startswith(JARVIS_NS) or low == PAGE or low.startswith(PAGE + "/") or \
                    (low == INBOX and not inbox):
                continue
            for b in p.blocks:
                lines = b.text.splitlines()
                if lines and lines[0].strip():
                    props = {m[1].lower(): m[2] for line in lines[1:] if (m := PROP.match(line))}
                    out.append((lines[0].strip(), {**props, "page": p.name}))
        return out

    def _digest(self) -> str:
        return hashlib.sha1("\n".join(sorted(f"{p['page']}|{t}" for t, p in self.kept())).encode()).hexdigest()

    def context(self, limit: int = CONTEXT_CHARS) -> str:
        """The short version for the system prompt, read from the memory pages each time (a correction there is
        never out of date here): who the user is (CORE pages, in that order), then the newest of the rest (a
        capture on jarvis/inbox counts before a dream went through it)."""
        items = self.kept(inbox=True)
        core = [e for c in CORE for e in items if e[1]["page"].lower() == c]
        rest = sorted((e for e in items if e[1]["page"].lower() not in CORE), key=lambda e: _learned(e[1]),
                      reverse=True)
        lines, size = [], 0
        for text, _ in core + rest:
            if size + len(text) + 3 <= limit:
                lines.append(f"- {text}")
                size += len(text) + 3
        return "\n".join(lines)

    async def run(self, *, dry_run: bool = False, days: int | None = None, today: date | None = None,
                  force: bool = False) -> Report:
        if self._lock.locked():
            raise RuntimeError("a dream is already running")
        async with self._lock:
            try:
                return await self._dream(dry_run=dry_run, days=days, today=today or date.today(), force=force)
            except Exception as e:  # a bad night is reported, never a crash (and never a half-written page)
                log.exception("dreaming failed")
                return Report(started=time.time(), since="", until=(today or date.today()).isoformat(),
                              dry_run=dry_run, error=str(e) or type(e).__name__)

    async def _dream(self, *, dry_run: bool, days: int | None, today: date, force: bool) -> Report:
        t0 = time.time()
        last = self.state.get("until")
        since = today - timedelta(days=(days or self.days) - 1)
        if not days and last and not force:  # carry on from the last dream, but look at least at today
            since = max(since, min(date.fromisoformat(last), today))
        rep = Report(started=t0, since=since.isoformat(), until=today.isoformat(), dry_run=dry_run,
                     model=str(getattr(self.llm, "model", "")))
        bits = gather(self.graph, self.traces, since, today)
        rep.gathered = len(bits)
        before = self._digest()
        self._tidy(rep, dry_run)  # needs no model: runs every night, quiet or not
        kept = self.kept()
        kept_words = [words(t) for t, _ in kept]
        known, cands = [], []  # kept already, as said / not yet
        for c in cluster(bits):
            (known if any(similar(words(c.text), kw) >= 0.9 for kw in kept_words) else cands).append(c)
        span = (today - since).days + 1
        for c in cands:
            score(c, today, span)
            if any(b.where == INBOX for b in c.bits):
                c.score = max(c.score, 0.9)  # captured on purpose: always put to the model
        clash = conflicts(cands, [t for t, _ in kept])  # among all of them, not only the best
        cands = sorted((c for c in cands if c.score >= MIN_SCORE), key=lambda c: -c.score)[:MAX_CANDIDATES]
        rep.candidates = len(cands)
        free, pairs, cited = [], [], set()
        # wake gate: nothing new said and the memory pages as they were last night asks no model
        if not cands and self.state.get("memory") == before and not force:
            rep.woke = False
        else:
            recurring = themes(bits)
            try:
                out = await self._deep(cands, kept[:MAX_ENTRIES], clash)
            except Exception as e:  # the model failed: keep the best as they were said (append-only, as OpenClaw does)
                log.warning("dreaming: the model failed (%s); adding the best candidates as said", e)
                out, rep.fallback = {"decisions": [{"op": "add", "cites": [f"C{i + 1}"], "text": c.text}
                                                   for i, c in enumerate(cands[:MAX_NEW])]}, True
            rep.model = getattr(self, "asked_model", rep.model)
            # a conflict is asked about only when the model confirms the pair can't both be true: the word match
            # alone asked whether "cloudcover 2022 to 2024" or "Energy Exemplar since 2024" was right (2026-11-14)
            ids = {**{f"C{i + 1}": c.text for i, c in enumerate(cands)},
                   **{f"E{i + 1}": t for i, (t, _) in enumerate(kept[:MAX_ENTRIES])}}
            pairs = list(dict.fromkeys((ids[a], ids[b]) for p in out.get("conflicts") or [] if isinstance(p, list)
                                       and len(p) == 2 and (a := str(p[0]).strip().upper()) in ids
                                       and (b := str(p[1]).strip().upper()) in ids and a != b
                                       # ...about one subject: the model alone paired "My wife's name is Sanika"
                                       # with where she used to work, which share no word (2026-11-14)
                                       and words(ids[a]) & words(ids[b])))[:2]
            disputed = {t for pair in (clash if rep.fallback else pairs) for t in pair}
            cited = self._apply(out, cands, kept[:MAX_ENTRIES], rep, today, disputed, dry_run)
            rep.themes = _sentences(str(out.get("themes") or ""), 320) or \
                (f"Recurring: {', '.join(recurring)}." if recurring else "")
            free = [str(q).strip() for q in out.get("questions") or [] if str(q).strip()][:2]
        rep.questions = self._questions(free, pairs, today, keep=not dry_run,
                                        more=self._stale(today) + self._clashes())
        rep.orphans = self._orphans()
        if not dry_run:
            # captures gone through (kept, skipped or kept already) leave the inbox; the rest wait for tomorrow
            done = {b.text for i, c in enumerate(cands) if f"C{i + 1}" in cited for b in c.bits if b.where == INBOX}
            done |= {b.text for c in known for b in c.bits if b.where == INBOX}
            for t in done:
                self._drop(INBOX, t)
            rep.inbox = len(done)
            self._diary(rep, today)
        rep.seconds = round(time.time() - t0, 2)
        self._finish(rep, dry_run, today)
        return rep

    async def _deep(self, cands: list[Candidate], kept: list[tuple[str, dict]], clash) -> dict:
        lines = ["Kept now:"] + ([f"E{i + 1} [{p['page'].removeprefix(JARVIS_NS)}]: {t}"
                                  for i, (t, p) in enumerate(kept)] or ["(nothing yet)"])
        lines.append("\nCandidates:")
        for i, c in enumerate(cands):
            where = "; ".join(sorted({f"{b.kind}{' (the user)' if b.trusted else ''} {b.day:%d %B %Y}" for b in c.bits}))
            said = " | ".join(dict.fromkeys(b.text for b in c.bits))[:400]
            lines.append(f"C{i + 1} [{where}; score {c.score}]: {said}")
        if clash:
            lines.append("\nThese may contradict each other:")
            lines += [f"- {a} / {b}" for a, b in clash]
        messages = [{"role": "system", "content": DEEP_PROMPT}, {"role": "user", "content": "\n".join(lines)}]
        if self.model and self.model != getattr(self.llm, "model", ""):
            llm = copy.copy(self.llm)  # the same connections, another model; the chat model is left alone
            llm.model = self.model
            for attempt in (1, 2):
                try:
                    return await self._ask(llm, messages)
                except Exception as e:
                    # "rate limited (try again in 0 s)": a short wait beats a slow chat model on a long prompt
                    wait = re.search(r"try again in (\d+) s", str(e))
                    if attempt == 1 and wait and int(wait[1]) <= RATE_WAIT_S:
                        log.info("dreaming: %s rate limited; trying again in %s s", self.model, int(wait[1]) + 1)
                        await asyncio.sleep(int(wait[1]) + 1)
                        continue
                    log.warning("dreaming: %s failed (%s); asking the chat model", self.model, e)
                    break
        return await self._ask(self.llm, messages)

    async def _ask(self, llm, messages: list[dict]) -> dict:
        self.asked_model = str(getattr(llm, "model", ""))
        messages = [dict(m) for m in messages]
        try:
            return await llm.json(messages, DEEP_SCHEMA)
        except Exception as e:  # a model without schema-constrained output: ask for JSON in words
            log.info("dreaming: no JSON mode (%s); asking for JSON", e)
            messages[0]["content"] += ("\nReply with JSON only, shaped like: {\"decisions\": [{\"op\": \"add\", "
                                       "\"cites\": [\"C1\"], \"entry\": \"\", \"topic\": \"family\", \"text\": \"...\"}], "
                                       "\"themes\": \"...\", \"conflicts\": [], \"questions\": []}")
            msg = await llm.chat(messages)
            raw = (msg.get("content") or "") if isinstance(msg, dict) else str(msg)
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                raise ValueError("no JSON in the reply") from None
            return json.loads(m[0])

    def _apply(self, out: dict, cands: list[Candidate], kept: list[tuple[str, dict]], rep: Report,
               today: date, disputed: set[str] = frozenset(), dry_run: bool = False) -> set[str]:
        """The model's decisions, checked: each must cite real facts or candidates, say nothing they don't, and
        stay within the night's limits. Each one passed is made on the memory pages (unless dry_run) and goes
        into rep.changes, so the morning digest can undo it. Returns the ids gone through (made or skipped)."""
        from jarvis_hub.kb.tools import TOPICS, memory_topic
        by_c = {f"C{i + 1}": c for i, c in enumerate(cands)}
        by_e = {f"E{i + 1}": e for i, e in enumerate(kept)}
        changes = new = 0
        used: set[str] = set()
        skipped: set[str] = set()
        for d in out.get("decisions") or []:
            op = str(d.get("op", "")).lower()
            ids = [str(x).strip().upper() for x in [*(d.get("cites") or []), d.get("entry") or ""]]
            cites = list(dict.fromkeys(x for x in ids if x in by_c or x in by_e))
            cs, es = [by_c[x] for x in cites if x in by_c], [x for x in cites if x in by_e]
            texts = [b.text for c in cs for b in c.bits] + [by_e[x][0] for x in es]
            if op == "skip" or not cites:
                rep.skipped += len(cites) if op == "skip" else 0
                skipped.update(cites if op == "skip" else [])
                continue
            if any(t in disputed for t in texts):  # a question for the user first
                rep.skipped += 1
                continue
            if any(_user(by_e[x][1]) for x in es):  # the user's own block: never merged away, rewritten or moved
                log.info("dreaming: left %s alone: the user wrote it", [by_e[x][0] for x in es if _user(by_e[x][1])])
                continue
            if used.intersection(cites) or changes >= MAX_CHANGES:  # a fact goes into one decision
                continue
            topic = str(d.get("topic") or "").strip().lower().removeprefix(JARVIS_NS)
            topic = memory_topic(topic) if topic and memory_topic(topic) in TOPICS and \
                (memory_topic(topic) != "memory" or "memory" in topic) else ""
            if op == "entity":
                name = re.sub(r"\s+", " ", str(d.get("text") or d.get("entry") or "")).strip()[:60]
                aliases = [a for a in (re.sub(r"\s+", " ", str(x)).strip()[:60] for x in d.get("aliases") or []) if a][:8]
                rels = [r for r in (_relation(str(x)) for x in d.get("relations") or []) if r][:8]
                page = ENTITIES + (slug(str(d.get("entry") or "")) or slug(name))
                facts = [by_e[x] for x in es if by_e[x][1]["page"].lower() != page]
                # its name and other names must be in its facts, as any name a memory gives
                if not name or page == ENTITIES or not facts or not grounded(" ".join([name, *aliases]), texts):
                    continue
                etype = str(d.get("type") or "").lower()
                front = {"type": etype if etype in ENTITY_TYPES else "other", "aliases": aliases, "relations": rels}
                rep.changes.append(self._change("entity", name, page, "\n".join(f"- {_raw(t, p)}" for t, p in facts),
                                                facts, dry_run, facts=[t for t, _ in facts], front=front))
                rep.entities.append(name)
                cites = es  # a candidate cited alongside isn't kept by this
            elif op == "move":
                if len(es) != 1 or cs or not topic or JARVIS_NS + topic == by_e[es[0]][1]["page"].lower():
                    continue
                t, p = by_e[es[0]]
                rep.changes.append(self._change("moved", t, JARVIS_NS + topic, _raw(t, p), [by_e[es[0]]], dry_run))
                rep.moved.append(t)
            else:
                text = re.sub(r"\s+", " ", str(d.get("text") or "")).strip()
                text = re.sub(r"\s*\((?:[^()]*\b[CE]\d+\b[^()]*)\)|\s*\[[CE]\d+\]", "", text).strip()  # "(from C2)"
                if not text or SECRET.search(text) or INSTRUCTION.search(text) or re.search(r"\b[CE]\d+\b", text) \
                        or len(text) > 240 or COMMAND.match(text) or len(re.findall(r"[.!?](?:\s|$)", text)) > 2:
                    continue
                if len(texts) > 1 and min((similar(words(a), words(b)) for i, a in enumerate(texts)
                                           for b in texts[i + 1:]), default=1) < 0.4:
                    log.info("dreaming: dropped %r: merges unrelated things", text)
                    continue
                # its sources, and the days they were said on: "tomorrow" may become an absolute date
                days = {b.day for c in cs for b in c.bits}
                sources = texts + [f"{d:%d %B %Y} {d:%b} {d.day} {d + timedelta(days=1):%d %B %Y} "
                                   f"{d + timedelta(days=1):%A %B}" for d in days]
                if not grounded(text, sources):
                    log.info("dreaming: dropped %r: says what its sources don't", text)
                    continue
                trusted = any(b.trusted for c in cs for b in c.bits) or \
                    any(by_e[x][1].get("via", "").lower().startswith(TRUSTED_VIA) for x in es)
                learned = min((by_e[x][1].get("learned", "") for x in es), key=lambda s: _day_of(
                    LINK.sub(lambda m: m[1], s)) or date.max, default="") or f"[[{journal_title(today)}]]"
                via = ("said by you" if trusted else "noted by JARVIS") + ", tidied by dreaming"
                raw = f"{text}\n  learned:: {learned}\n  via:: {via}"
                if not es:  # nothing kept says it yet: a new fact
                    if new >= MAX_NEW or any(similar(words(text), kw) >= 0.9 for kw in (words(t) for t, _ in kept)):
                        continue
                    page = JARVIS_NS + (topic or memory_topic("", text))
                    rep.changes.append(self._change("added", text, page, raw, [], dry_run))
                    rep.added.append(text)
                    new += 1
                else:  # merge (several say one thing) or supersede (corrected): one fact replaces them
                    kind = "merged" if len(es) + len(cs) > 1 and op == "merge" else "updated"
                    page = JARVIS_NS + topic if topic else by_e[es[0]][1]["page"]
                    rep.changes.append(self._change(kind, text, page, raw, [by_e[x] for x in es], dry_run))
                    (rep.merged if kind == "merged" else rep.updated).append(text)
            used.update(cites)
            changes += 1
        return used | skipped

    # ---------- changes on the memory pages, each undoable ----------
    def _change(self, op: str, text: str, page: str, raw: str, was: list[tuple[str, dict]], dry_run: bool,
                **more) -> dict:
        c = {"op": op, "text": text, "page": page, "raw": raw,
             "was": [{"page": p["page"], "text": t, "raw": _raw(t, p)} for t, p in was], **more}
        if not dry_run:
            self._do(c)
        return c

    def _tidy(self, rep: Report, dry_run: bool) -> None:
        """Near-duplicates on one page become one, no model asked: the user's own block stays as it is (another
        user block goes only as its exact copy); otherwise the user's words, then the longer, with the earliest
        learned date. Each is a "merged" change, undoable."""
        pages: dict[str, list[tuple[str, dict]]] = {}
        for t, p in self.kept():
            pages.setdefault(p["page"], []).append((t, p))
        for page, facts in pages.items():
            groups: list[list[tuple[str, dict]]] = []
            for f in facts:
                g = next((g for g in groups if duplicate(g[0][0], f[0])), None)
                g.append(f) if g else groups.append([f])
            for g in (g for g in groups if len(g) > 1):
                users = [f for f in g if _user(f[1])]
                keep = users[0] if users else max(g, key=lambda f: ("by you" in f[1].get("via", "").lower(), len(f[0])))
                gone = [f for f in g if f is not keep and (not _user(f[1]) or f[0] == keep[0])]
                if not gone:
                    continue
                if users and all(f[0] != keep[0] for f in gone):  # the user's block isn't touched
                    c = self._change("merged", keep[0], page, "", gone, dry_run, facts=[])
                else:  # rewritten with the earliest learned date (dropping one copy drops every copy)
                    learned = min((f[1].get("learned", "") for f in g), key=lambda s: _day_of(
                        LINK.sub(lambda m: m[1], s).strip()) or date.max)
                    props = keep[1] if users or not learned else {**keep[1], "learned": learned}
                    c = self._change("merged", keep[0], page, _raw(keep[0], props), [keep] + gone, dry_run)
                rep.changes.append(c)
                rep.merged.append(keep[0])

    def _set_entity(self, name: str, front: dict, drop: str = "") -> str | None:
        """Union front (type, aliases, relations) into the entity page's frontmatter, without the relation `drop`.
        Returns the frontmatter as it was ("" none; None: there was no page)."""
        page = self.graph.page(name, exact=True)
        text = page.path.read_text(encoding="utf-8") if page else ""
        m = FRONTMATTER.match(text)
        props = page.properties if page else {}
        other, key = [], None  # the frontmatter's other lines, as they were
        for line in (m[1].splitlines() if m else []):
            if kv := re.match(r"^([A-Za-z0-9_\-]+):", line):
                key = kv[1].lower()
            if key not in ("type", "aliases", "relations"):
                other.append(line)
        etype = props.get("type") or front.get("type") or ""
        aliases = _union(_items(props.get("aliases", "")), front.get("aliases") or [])
        rels = [r for r in _union(_items(props.get("relations", "")), front.get("relations") or []) if r != drop]
        head = [f"type: {etype}"] * bool(etype) + [f"aliases: [{', '.join(aliases)}]"] * bool(aliases) + \
            [f"relations: [{', '.join(rels)}]"] * bool(rels) + other
        body = text[m.end():] if m else text
        self.graph.replace_page(name, "---\n" + "\n".join(head) + "\n---\n" + body.lstrip("\n"))
        return (m[0] if m else "") if page else None

    def _unset_entity(self, name: str, was: str | None) -> None:
        """An entity change undone: its frontmatter as it was, or the page gone if the change made it and it is
        empty now."""
        page = self.graph.page(name, exact=True)
        if page is None:
            return
        if was is None and not page.blocks:
            self.graph.trash_jarvis_page(name)
        elif was is not None:
            text = page.path.read_text(encoding="utf-8")
            m = FRONTMATTER.match(text)
            self.graph.replace_page(name, was + (text[m.end():] if m else text).lstrip("\n"))

    def _drop(self, page: str, text: str) -> None:
        try:
            # the block's lines come joined by spaces: the fact itself, then only its properties ("learned:: ...")
            self.graph._drop_blocks(self.graph._owned(page), lambda t: t.startswith(text) and (
                not t[len(text):].strip() or bool(re.match(r"\s+[\w-]+::", t[len(text):]))))
        except ValueError:
            pass  # gone already (the user deleted it)

    def _do(self, c: dict) -> None:
        for w in c["was"]:
            self._drop(w["page"], w["text"])
        if c["op"] == "entity":
            c["front_was"] = self._set_entity(c["page"], c["front"])
        if c["raw"]:
            self.graph.write_jarvis_page(c["page"], c["raw"])

    def _undo(self, c: dict) -> None:
        for t in c.get("facts", [c["text"]]):
            self._drop(c["page"], t)
        for w in c["was"]:
            self.graph.write_jarvis_page(w["page"], w["raw"])
        if c["op"] == "entity":
            self._unset_entity(c["page"], c.get("front_was"))

    def undo(self, i: int) -> dict:
        """Take back one change of the last dream (the morning digest): an added fact goes, a merged, updated or
        moved one is back as it was. Undoing it again redoes it."""
        changes = (self.state.get("last") or {}).get("changes") or []
        if not 0 <= i < len(changes):
            raise IndexError("no such change")
        c = changes[i]
        if "raw" not in c:
            raise IndexError("that change is from the old dreams page; it can't be undone here")
        (self._do if c.get("undone") else self._undo)(c)
        c["undone"] = not c.get("undone")
        self.state["memory"] = self._digest()
        self._save()
        if self.on_change:
            self.on_change()
        return c

    # ---------- questions for the user: asked once, answered in the morning digest ----------
    def _questions(self, free: list[str], pairs: list[tuple[str, str]], today: date, keep: bool,
                   more: list[dict] = ()) -> list[str]:
        """more: questions built elsewhere ({q, ...}); a stale fact's (with "fact") is checked for repeats there."""
        qs = self.state.setdefault("questions", []) if keep else list(self.state.get("questions") or [])
        new = []

        def asked(text: str) -> bool:
            return any(similar(words(text), words(q["q"])) >= 0.8 for q in qs)
        for a, b in pairs:
            q = f'These disagree; which is right? "{a}" / "{b}"'
            if not any({a, b} == {x.get("a"), x.get("b")} for x in qs) and not asked(q):
                qs.append({"id": uuid.uuid4().hex[:8], "q": q, "a": a, "b": b, "status": "open",
                           "asked": today.isoformat()})
                new.append(q)
        for q in free:
            if not asked(q):
                qs.append({"id": uuid.uuid4().hex[:8], "q": q, "status": "open", "asked": today.isoformat()})
                new.append(q)
        for x in more:
            if x.get("fact") or not asked(x["q"]):
                qs.append({"id": uuid.uuid4().hex[:8], **x, "status": "open", "asked": today.isoformat()})
                new.append(x["q"])
        del qs[:-MAX_QUESTIONS]
        return new

    def _stale(self, today: date) -> list[dict]:
        """Facts that change (where the user lives and works, their tools and devices) not confirmed for
        STALE_DAYS: "Is this still true?", at most MAX_STALE a night and never the same fact twice in that time."""
        qs = self.state.get("questions") or []
        types = {p.name.lower(): p.properties.get("type", "").lower() for p in self.graph.pages()
                 if p.name.lower().startswith(ENTITIES)}
        out = []
        for t, p in self.kept():
            page = p["page"].lower()
            if page not in VOLATILE and types.get(page) not in ("device", "place"):
                continue
            when = _learned({"learned": p.get("as_of") or p.get("learned", "")})
            if when == date.min or (today - when).days <= STALE_DAYS or any(
                    q.get("fact") == t and (today - date.fromisoformat(q["asked"])).days < STALE_DAYS for q in qs):
                continue
            out.append({"q": f"Is this still true: {t.rstrip('.')}?", "fact": t, "page": p["page"],
                        "choices": ["yes", "no", "text"]})
            if len(out) >= MAX_STALE:
                break
        return out

    def _clashes(self) -> list[dict]:
        """An entity related to two different targets where there is only one (located_at, lives_in...)."""
        out = []
        for page in self.graph.pages():
            if not page.name.lower().startswith(ENTITIES):
                continue
            first: dict[str, str] = {}
            for r in _items(page.properties.get("relations", "")):
                kind = r.split(" ", 1)[0].lower()
                if kind in SINGLE and first.setdefault(kind, r) != r:
                    name = page.name.rsplit("/", 1)[-1]
                    a, b = f"{name} {first[kind]}", f"{name} {r}"
                    out.append({"q": f'These disagree; which is right? "{a}" / "{b}"', "a": a, "b": b,
                                "entity": page.name, "rel_a": first[kind], "rel_b": r})
        return out

    def _orphans(self) -> list[str]:
        """jarvis pages with nothing on them that no page links to (the diary notes them; the user decides)."""
        pages = self.graph.pages()
        linked = set().union(*(p.links for p in pages))
        return [p.name for p in pages if not p.is_journal and not p.blocks and p.name.lower().startswith(JARVIS_NS)
                and p.name.lower() not in (INBOX, PAGE) and p.name.lower() not in linked
                and p.name.lower().rsplit("/", 1)[-1] not in linked]

    def open_questions(self) -> list[dict]:
        return [q for q in self.state.get("questions") or [] if q.get("status") == "open"]

    def answer(self, qid: str, choice: str, text: str = "") -> dict:
        """The user's answer from the morning digest. choice: "a" or "b" (which of two disagreeing facts is
        right: the other is forgotten), "text" (their answer, saved as a fact), or "dismiss" (never asked again).
        A stale fact's question ("Is this still true?") also takes "yes" (its as_of becomes today) and "no" (as
        dismiss); its "text" answer replaces the fact. An entity's two targets: "a"/"b" drops the other relation."""
        from jarvis_hub.kb.tools import memory_topic
        q = next((q for q in self.state.get("questions") or [] if q["id"] == qid), None)
        if q is None:
            raise KeyError(qid)
        today = f"[[{journal_title(date.today())}]]"
        old = next(((t, p) for t, p in self.kept() if t == q.get("fact") and p["page"] == q.get("page")), None)
        if choice in ("a", "b") and q.get("entity"):
            self._set_entity(q["entity"], {}, drop=q["rel_b"] if choice == "a" else q["rel_a"])
            q["answer"] = q[choice]
        elif choice == "yes" and q.get("fact"):
            if old:
                self._drop(q["page"], old[0])
                self.graph.write_jarvis_page(q["page"], _raw(old[0], {**old[1], "as_of": today}))
            q["answer"] = "yes"
        elif choice == "text" and q.get("fact") and (said := " ".join(text.split())[:300]):
            if SECRET.search(said) or INSTRUCTION.search(said):
                raise ValueError("that answer can't be kept as a memory")
            if old:
                self._drop(q["page"], old[0])
            self.graph.write_jarvis_page(q["page"], f"{said}\n  learned:: {today}\n  as_of:: {today}\n  via:: answered by you")
            q["answer"] = said
        elif choice in ("a", "b") and q.get("a"):
            right, wrong = (q["a"], q["b"]) if choice == "a" else (q["b"], q["a"])
            for t, p in self.kept():
                if similar(words(t), words(wrong)) >= 0.9 and similar(words(t), words(right)) < 0.9:
                    self._drop(p["page"], t)
            if not any(similar(words(t), words(right)) >= 0.9 for t, _ in self.kept()):
                self.graph.write_jarvis_page(memory_topic("", right),
                                             f"{right}\n  learned:: {today}\n  via:: answered by you")
            q["answer"] = right
        elif choice == "text" and (said := " ".join(text.split())[:300]):
            if SECRET.search(said) or INSTRUCTION.search(said):
                raise ValueError("that answer can't be kept as a memory")
            self.graph.write_jarvis_page(memory_topic("", said), f"{said}\n  learned:: {today}\n  via:: answered by you")
            q["answer"] = said
        elif choice not in ("dismiss", "no"):
            raise ValueError("choice is a, b, yes, no, text (with the answer) or dismiss")
        q["status"] = "dismissed" if choice in ("dismiss", "no") else "answered"
        self.state["memory"] = self._digest()
        self._save()
        if self.on_change and choice not in ("dismiss", "no"):
            self.on_change()
        return q

    def _diary(self, rep: Report, today: date) -> None:
        """One entry per day on one page: a night with nothing changed and no new question writes nothing, a
        second run the same day replaces that day's entry, old days roll off."""
        page = self.graph.page(DIARY)
        old = page.path.read_text(encoding="utf-8") if page else ""
        mark = f"- Dream of [[{journal_title(today)}]]"
        days = [d for d in re.split(r"(?m)^(?=- Dream of )", old) if d.startswith("- Dream of ")]
        others = [d for d in days if not d.startswith(mark)]
        kids = [f"{label}: {t}" for label, items in (("Kept", rep.added), ("Merged", rep.merged),
                                                     ("Updated", rep.updated), ("Moved", rep.moved),
                                                     ("Given its own page", rep.entities)) for t in items]
        kids += [f"Question for you: {q}" for q in rep.questions]
        if not kids:
            return
        kids += [f"Empty page nothing links to: {p}" for p in rep.orphans]  # plain name: a link would adopt it
        if rep.themes:
            kids.insert(0, f"Themes: {rep.themes}")
        head = (f"{mark}: went through {rep.gathered} notes and things you said from "
                f"{rep.since} to {rep.until}; {rep.candidates} looked worth a thought"
                + (" (the model failed, so the best were kept as said)" if rep.fallback else ""))
        entry = head + "\n" + "\n".join(f"\t- {k}" for k in kids) + "\n"
        days = (others + [entry])[-DIARY_DAYS:]
        self.graph.write_jarvis_page(DIARY, "".join(d if d.endswith("\n") else d + "\n" for d in days), replace=True)

    def _finish(self, rep: Report, dry_run: bool, today: date) -> None:
        if dry_run:
            return
        self.state.update(last_run=rep.started, until=today.isoformat(), last=rep.__dict__, memory=self._digest())
        self._save()
        if self.on_change and (rep.changes or rep.inbox):  # the tidy step's merges too: the index re-syncs
            self.on_change()

    # ---------- schedule ----------
    def due(self, now: datetime | None = None) -> bool:
        """Past tonight's time and not run since (or missed while JARVIS was off: once on start)."""
        now = now or datetime.now()
        h, m = (int(x) for x in self.at.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < slot:
            slot -= timedelta(days=1)
        return float(self.state.get("last_run") or 0) < slot.timestamp()

    async def _loop(self) -> None:
        await asyncio.sleep(120)  # let the hub settle after a start
        while True:
            if self.due() and not self.running:
                try:
                    rep = await self.run()
                    log.info("dreamed: %d gathered, %d candidates, +%d ~%d =%d", rep.gathered, rep.candidates,
                             len(rep.added), len(rep.updated), len(rep.merged))
                except Exception:
                    log.exception("dreaming failed")
                    self.state["last_run"] = time.time()  # don't retry in a loop; try again tomorrow
                    self._save()
            await asyncio.sleep(300)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


def _sentences(text: str, limit: int) -> str:
    """Whole sentences of text, at most limit characters."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > 0 else cut.rsplit(" ", 1)[0] + "…"


def summary(rep: Report) -> str:
    """A report in a few words, as JARVIS says it."""
    if not rep.woke and not rep.changes and not rep.questions:
        return "Nothing new since my last dream, sir, so there was nothing to go through."
    if rep.error:
        return f"I couldn't dream, sir: {rep.error}"
    done = [f"kept {len(rep.added)} new thing{'s' * (len(rep.added) != 1)}" if rep.added else "",
            f"merged {len(rep.merged)}" if rep.merged else "", f"updated {len(rep.updated)}" if rep.updated else "",
            f"moved {len(rep.moved)}" if rep.moved else "",
            f"gave {len(rep.entities)} its own page" if rep.entities else ""]
    done = [d for d in done if d]
    out = (f"I went through {rep.gathered} notes and things you said and " +
           (", ".join(done) if done else "found nothing to change") + ", sir.")
    if kept := rep.added + rep.updated + rep.merged:
        out += " " + " ".join(kept[:3])
    if rep.questions:
        out += f" One question: {rep.questions[0]}"
    return out


def register_dream_tool(reg: ToolRegistry, dreamer: Dreamer) -> None:
    async def dream(args: dict, ctx: ToolCtx) -> str:
        if dreamer.running:
            return "I'm dreaming already, sir; the diary will have it shortly."
        try:
            rep = await dreamer.run(days=int(args["days"]) if str(args.get("days") or "").isdigit() else None)
        except Exception as e:
            log.exception("dream failed")
            return f"ERROR: the dream failed: {e}. Tell the user it failed; don't retry now."
        return summary(rep)

    reg.add(Tool("dream", "builtin",
                 "Go through the last days' notes and conversations now and tidy the memory pages: keep what matters, "
                 "merge duplicates, fix what was corrected "
                 "(it also runs every night). Use for: 'dream now', 'consolidate your memory', 'tidy up what you know'.",
                 {"type": "object", "properties": {"days": {"type": "integer", "description": "how many days back (default 3)"}},
                  "required": []}, Risk.WRITE, dream, self_scoped=True, final_answer=True))
