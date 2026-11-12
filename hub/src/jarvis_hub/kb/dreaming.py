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

Everything goes to jarvis/dreams (what is kept) and jarvis/dreams/diary (each night's report). Other pages
are only read. Each rewrite of jarvis/dreams keeps the old page in .trash/."""

import asyncio
import copy
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from jarvis_hub.approval.gate import Risk
from jarvis_hub.kb.graph import JARVIS_NS, JOURNAL_FILE, LINK, PROP, ObsidianVault, journal_title
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

log = logging.getLogger(__name__)

PAGE = "jarvis/dreams"
DIARY = "jarvis/dreams/diary"
WEIGHTS = {"relevance": 0.30, "frequency": 0.24, "diversity": 0.15, "recency": 0.15, "consolidation": 0.10,
           "richness": 0.06}
MIN_SCORE = 0.35  # a candidate below this isn't put to the model
MAX_CANDIDATES = 12
MAX_CHANGES, MAX_NEW = 6, 3  # per night (Hermes)
MAX_KEPT = 40  # jarvis/dreams holds at most this many memories; the lowest-scored go first
CONTEXT_CHARS = 700  # what rides in every conversation
HALF_LIFE_DAYS = 7
SAME = 0.6  # share of words two texts must have in common to be the same thing
TRUSTED_VIA = ("said by you", "corrected by you", "edited by you")
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

DEEP_PROMPT = """You consolidate a home assistant's memory of its user. You get what is kept now (E1, E2...) and
new candidates from the last days (C1, C2...), each with where it came from. Decide what to keep:
- add: a candidate worth keeping that nothing kept says yet. text: one plain sentence.
- merge: candidates that say the same thing (and maybe a kept entry): one sentence for all of them.
- supersede: a candidate that corrects or updates a kept entry: the corrected sentence replaces it.
- skip: chit-chat, one-off questions, things only true for a moment, guesses, and requests to the assistant
  (reminders, timers, messages, "remember to remind me..."): those are handled elsewhere, not memories.
Keep what will still matter in a month: people and their names, dates, preferences, plans, projects, places.
Write each text as a fact about the user ("The user's wife is Sanika."), with full dates that include the year
("on 3 November 2026"; never "today", "tomorrow" or a date without its year: each candidate shows the day it
was said). Birthdays and anniversaries recur every year: give them without a year ("born on 13 May"). Use only names, numbers and dates that are in the cited candidates or entry; never add any.
Every decision cites the candidates it rests on. themes: one or two sentences on what the user has been
occupied with. questions: things that contradict each other or look wrong, as short questions to the user."""
DEEP_SCHEMA = {"type": "object", "properties": {
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["add", "merge", "supersede", "skip"]},
        "cites": {"type": "array", "items": {"type": "string"}},
        "entry": {"type": "string"},
        "text": {"type": "string"}}, "required": ["op", "cites"]}},
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
        if page.is_journal:
            m = JOURNAL_FILE.match(page.path.name)
            day = date(int(m[1]), int(m[2]), int(m[3])) if m else None
            if not day or not since <= day <= until:
                continue
            for b in page.blocks:
                ours = "#jarvis" in b.text.lower() or "jarvis" in b.links
                if (text := _clean(b.text)) and not COMMAND.match(text):
                    bits.append(Bit(text, day, "journal", not ours, page.name))
        elif low.startswith(JARVIS_NS):
            for b in page.blocks:
                props = {m[1].lower(): m[2] for line in b.text.splitlines() if (m := PROP.match(line))}
                day = _day_of(LINK.sub(lambda m: m[1], props.get("learned", "")).strip())
                if not day or not since <= day <= until:
                    continue
                via = props.get("via", "").lower()
                if (text := _clean(b.text)) and not COMMAND.match(text):
                    bits.append(Bit(text, day, "memory", via.startswith(TRUSTED_VIA), page.name))
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
    skipped: int = 0
    questions: list[str] = field(default_factory=list)
    themes: str = ""
    model: str = ""
    fallback: bool = False
    dry_run: bool = False
    woke: bool = True  # false: nothing new, no model was asked
    seconds: float = 0.0
    error: str = ""
    # each change on its own, for the morning digest's undo: {op: added|merged|updated|dropped, text, was}
    changes: list[dict] = field(default_factory=list)


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
        self.state_file.write_text(json.dumps(self.state, indent=1, default=str), encoding="utf-8")

    @property
    def running(self) -> bool:
        return self._lock.locked()

    # what is kept: [(text, props)] from jarvis/dreams
    def kept(self) -> list[tuple[str, dict]]:
        page = self.graph.page(PAGE)
        out = []
        for b in page.blocks if page else []:
            lines = b.text.splitlines()
            props = {m[1].lower(): m[2] for line in lines[1:] if (m := PROP.match(line))}
            out.append((lines[0].strip(), props))
        return out

    def context(self, limit: int = CONTEXT_CHARS) -> str:
        """The short version for the system prompt: the best-kept memories, most corroborated first."""
        kept = sorted(self.kept(), key=lambda e: -float(e[1].get("score", "0") or 0))
        lines, size = [], 0
        for text, _ in kept:
            if size + len(text) + 3 > limit:
                break
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
        kept = self.kept()
        kept_words = [words(t) for t, _ in kept]
        cands = [c for c in cluster(bits)
                 if not any(similar(words(c.text), kw) >= 0.9 for kw in kept_words)]  # kept already, as said
        span = (today - since).days + 1
        for c in cands:
            score(c, today, span)
        clash = conflicts(cands, [t for t, _ in kept])  # among all of them, not only the best
        cands = sorted((c for c in cands if c.score >= MIN_SCORE), key=lambda c: -c.score)[:MAX_CANDIDATES]
        rep.candidates = len(cands)
        if not cands and not force:  # wake gate: a quiet night asks no model
            rep.woke, rep.seconds = False, round(time.time() - t0, 2)
            self._finish(rep, dry_run, today)
            return rep
        recurring = themes(bits)
        try:
            out = await self._deep(cands, kept, clash)
        except Exception as e:  # the model failed: keep the best as they were said (append-only, as OpenClaw does)
            log.warning("dreaming: the model failed (%s); adding the best candidates as said", e)
            out, rep.fallback = {"decisions": [{"op": "add", "cites": [f"C{i + 1}"], "text": c.text}
                                               for i, c in enumerate(cands[:MAX_NEW])]}, True
        rep.model = getattr(self, "asked_model", rep.model)
        entries = self._apply(out, cands, kept, rep, today, {t for pair in clash for t in pair})
        rep.themes = _sentences(str(out.get("themes") or ""), 320) or \
            (f"Recurring: {', '.join(recurring)}." if recurring else "")
        rep.questions = [str(q).strip() for q in out.get("questions") or [] if str(q).strip()][:4] + \
            [f"These disagree; which is right? \"{a}\" / \"{b}\"" for a, b in clash
             if not any(a[:20] in str(q) or b[:20] in str(q) for q in out.get("questions") or [])][:2]
        if not dry_run:
            if rep.added or rep.merged or rep.updated:
                self._write(entries)
            self._diary(rep, today)
        rep.seconds = round(time.time() - t0, 2)
        self._finish(rep, dry_run, today)
        return rep

    async def _deep(self, cands: list[Candidate], kept: list[tuple[str, dict]], clash) -> dict:
        lines = ["Kept now:"] + ([f"E{i + 1}: {t}" for i, (t, _) in enumerate(kept)] or ["(nothing yet)"])
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
            try:
                return await self._ask(llm, messages)
            except Exception as e:
                log.warning("dreaming: %s failed (%s); asking the chat model", self.model, e)
        return await self._ask(self.llm, messages)

    async def _ask(self, llm, messages: list[dict]) -> dict:
        self.asked_model = str(getattr(llm, "model", ""))
        messages = [dict(m) for m in messages]
        try:
            return await llm.json(messages, DEEP_SCHEMA)
        except Exception as e:  # a model without schema-constrained output: ask for JSON in words
            log.info("dreaming: no JSON mode (%s); asking for JSON", e)
            messages[0]["content"] += ("\nReply with JSON only, shaped like: {\"decisions\": [{\"op\": \"add\", "
                                       "\"cites\": [\"C1\"], \"entry\": \"\", \"text\": \"...\"}], \"themes\": \"...\", "
                                       "\"questions\": []}")
            msg = await llm.chat(messages)
            raw = (msg.get("content") or "") if isinstance(msg, dict) else str(msg)
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                raise ValueError("no JSON in the reply") from None
            return json.loads(m[0])

    def _apply(self, out: dict, cands: list[Candidate], kept: list[tuple[str, dict]], rep: Report,
               today: date, disputed: set[str] = frozenset()) -> list[tuple[str, dict]]:
        """The model's decisions, checked: each must cite real candidates, say nothing they don't, and stay within
        the night's limits. What is kept now, with these applied."""
        entries = [(t, dict(p)) for t, p in kept]
        by_id = {f"C{i + 1}": c for i, c in enumerate(cands)}
        changes = new = 0
        used: set[str] = set()
        for d in out.get("decisions") or []:
            op = str(d.get("op", "")).lower()
            cites = list(dict.fromkeys(c for c in (str(x).strip().upper() for x in d.get("cites") or []) if c in by_id))
            if any(b.text in disputed for c in cites for b in by_id[c].bits):  # a question for the user first
                rep.skipped += 1
                continue
            if used.intersection(cites):  # a candidate goes into one decision; a second one rests on it twice
                continue
            text = re.sub(r"\s+", " ", str(d.get("text") or "")).strip()
            if op == "skip" or not cites:
                rep.skipped += len(cites) if op == "skip" else 0
                continue
            text = re.sub(r"\s*\((?:[^()]*\b[CE]\d+\b[^()]*)\)|\s*\[[CE]\d+\]", "", text).strip()  # "(from C2)"
            if changes >= MAX_CHANGES or not text or SECRET.search(text) or INSTRUCTION.search(text) or \
                    re.search(r"\b[CE]\d+\b", text) or len(text) > 240 or COMMAND.match(text) or \
                    len(re.findall(r"[.!?](?:\s|$)", text)) > 2:
                continue
            if len(cites) > 1 and min((similar(words(by_id[a].text), words(by_id[b].text))
                                       for a in cites for b in cites if a < b), default=1) < 0.4:
                log.info("dreaming: dropped %r: merges unrelated things", text)
                continue
            entry = str(d.get("entry") or "").strip().upper()
            e_i = int(entry[1:]) - 1 if re.fullmatch(r"E\d+", entry) and int(entry[1:]) <= len(entries) else None
            # its sources, and the days they were said on: "tomorrow" may become an absolute date
            sources = [b.text for c in cites for b in by_id[c].bits] + ([entries[e_i][0]] if e_i is not None else []) + \
                [f"{d:%d %B %Y} {d:%b} {d.day} {d + timedelta(days=1):%d %B %Y} {d + timedelta(days=1):%A %B}"
                 for d in {b.day for c in cites for b in by_id[c].bits}]
            if not grounded(text, sources):
                log.info("dreaming: dropped %r: says what its sources don't", text)
                continue
            cs = [by_id[c] for c in cites]
            props = {"dreamed": f"[[{journal_title(today)}]]",
                     "from": ", ".join(sorted({f"{'your words' if b.trusted else 'JARVIS'} ({b.kind}, {b.day:%d %b})"
                                               for c in cs for b in c.bits}))[:200],
                     "score": f"{max(c.score for c in cs):.2f}"}
            if e_i is not None and op in ("supersede", "merge"):
                old = entries[e_i][1]
                props["score"] = f"{max(float(old.get('score', 0) or 0), float(props['score'])):.2f}"
                rep.changes.append({"op": "updated" if op == "supersede" else "merged", "text": text,
                                    "was": entries[e_i][0], "was_props": dict(old)})
                entries[e_i] = (text, props)
                (rep.updated if op == "supersede" else rep.merged).append(text)
            else:
                if new >= MAX_NEW:
                    continue
                if any(similar(words(text), words(t)) >= 0.9 for t, _ in entries):
                    continue
                entries.append((text, props))
                new += 1
                (rep.merged if op == "merge" and len(cites) > 1 else rep.added).append(text)
                rep.changes.append({"op": "merged" if op == "merge" and len(cites) > 1 else "added", "text": text})
            used.update(cites)
            changes += 1
        entries.sort(key=lambda e: -float(e[1].get("score", 0) or 0))
        rep.changes += [{"op": "dropped", "text": t, "was_props": p} for t, p in entries[MAX_KEPT:]]
        return entries[:MAX_KEPT]

    def _write(self, entries: list[tuple[str, dict]]) -> None:
        text = "\n".join(f"- {t}\n" + "".join(f"  {k}:: {v}\n" for k, v in p.items()) for t, p in entries)
        page = self.graph.page(PAGE)
        if page is not None:  # the page as it was is kept in .trash/
            self.graph._trash_path(page.path.name).write_text(page.path.read_text(encoding="utf-8"), encoding="utf-8")
        self.graph.write_jarvis_page(PAGE, text, replace=True)

    def _diary(self, rep: Report, today: date) -> None:
        if not rep.woke:
            return
        kids = []
        if rep.themes:
            kids.append(f"Themes: {rep.themes}")
        for label, items in (("Kept", rep.added), ("Merged", rep.merged), ("Updated", rep.updated)):
            kids += [f"{label}: {t}" for t in items]
        kids += [f"Question for you: {q}" for q in rep.questions]
        if not (rep.added or rep.merged or rep.updated):
            kids.append("Nothing new worth keeping.")
        head = (f"- Dream of [[{journal_title(today)}]]: went through {rep.gathered} notes and things you said from "
                f"{rep.since} to {rep.until}; {rep.candidates} looked worth a thought"
                + (" (the model failed, so the best were kept as said)" if rep.fallback else ""))
        self.graph.write_jarvis_page(DIARY, head + "\n" + "\n".join(f"\t- {k}" for k in kids))

    def _finish(self, rep: Report, dry_run: bool, today: date) -> None:
        if dry_run:
            return
        self.state.update(last_run=rep.started, until=today.isoformat(), last=rep.__dict__)
        self._save()
        if self.on_change and (rep.added or rep.merged or rep.updated):
            self.on_change()

    def undo(self, i: int) -> dict:
        """Take back one change of the last dream (the morning digest): an added fact goes, an updated or merged
        one gets its old wording back, a dropped one returns. Undoing it again redoes it."""
        changes = (self.state.get("last") or {}).get("changes") or []
        if not 0 <= i < len(changes):
            raise IndexError("no such change")
        c = changes[i]
        entries = self.kept()
        at = next((n for n, (t, _) in enumerate(entries) if t == c["text"]), None)
        was = next((n for n, (t, _) in enumerate(entries) if t == c.get("was")), None)
        if not c.get("undone"):
            if c["op"] == "dropped":
                entries.append((c["text"], c.get("was_props") or {}))
            elif c.get("was") and at is not None:
                entries[at] = (c["was"], c.get("was_props") or entries[at][1])
            elif at is not None:
                entries.pop(at)
        else:
            if c["op"] == "dropped" and at is not None:
                entries.pop(at)
            elif c.get("was") and was is not None:
                entries[was] = (c["text"], entries[was][1])
            elif c["op"] != "dropped" and at is None:
                entries.append((c["text"], {}))
        c["undone"] = not c.get("undone")
        self._write(entries)
        self._save()
        if self.on_change:
            self.on_change()
        return c

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
    if not rep.woke:
        return "Nothing new since my last dream, sir, so there was nothing to go through."
    if rep.error:
        return f"I couldn't dream, sir: {rep.error}"
    done = [f"kept {len(rep.added)} new thing{'s' * (len(rep.added) != 1)}" if rep.added else "",
            f"merged {len(rep.merged)}" if rep.merged else "", f"updated {len(rep.updated)}" if rep.updated else ""]
    done = [d for d in done if d]
    out = (f"I went through {rep.gathered} notes and things you said and " +
           (", ".join(done) if done else "found nothing new worth keeping") + ", sir.")
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
            return f"ERROR: the dream failed: {e}"
        return summary(rep)

    reg.add(Tool("dream", "builtin",
                 "Go through the last days' notes and conversations now and keep what matters on jarvis/dreams "
                 "(it also runs every night). Use for: 'dream now', 'consolidate your memory', 'tidy up what you know'.",
                 {"type": "object", "properties": {"days": {"type": "integer", "description": "how many days back (default 3)"}},
                  "required": []}, Risk.WRITE, dream, self_scoped=True, final_answer=True))
