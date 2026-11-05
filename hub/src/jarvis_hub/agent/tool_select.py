"""Per-request tool selection: a small model picks far better from ~9 tools than from 45 (TinyAgent,
RAG-MCP), so local models are offered only the tools closest in meaning to what the user just said,
plus a few that are always useful. A tool is matched by its description and by each example request
on its card (tools/cards.py): its score is the best of those, so one close example is enough. Tools in
the same family come along with the best one. Embeddings use the knowledge base's model; if embedding
fails, every tool is offered as before."""

import logging
import math
import re
from dataclasses import dataclass, field

from jarvis_hub.llm.client import LLMClient, LLMError
from jarvis_hub.mcp.registry import Tool
from jarvis_hub.tools.cards import card, family_of

log = logging.getLogger(__name__)
CORE = ("get_time", "calculate", "kb_search")  # cheap, and often needed alongside anything else
# Words that say which tool the user wants, whatever the embeddings score: "do a web search about REA"
# matched ask_opencode best ("how to use" ~ "use opencode to explain"), and a link matched nothing.
URL = re.compile(r"https?://\S+|\bwww\.\S+|\b[\w-]+\.(?:com|org|net|io|ai|dev|tools|in|co)(?:/\S*)?\b", re.I)
WANTS = ((URL, ("fetch_page", "web_search")),
         (re.compile(r"\b(?:web|online|internet|google|search (?:for|about|the)|look (?:it |that )?up|latest|news|"
                     r"github|reddit|youtube|stack ?overflow|wikipedia|documentation|docs)\b", re.I), ("web_search",)),
         # "search X" that doesn't say the notes: the web is offered beside search_notes (always offered)
         (re.compile(r"\bsearch\b(?![^.?!]*\b(?:notes?|memory|memories|journal|kb|knowledge base)\b)", re.I),
          ("web_search",)))
# Asked to keep something: the memory tool whatever the embeddings score ("also remember that we need a nightly
# job..." matched skills and dreaming, and remember wasn't offered).
WANTS += ((re.compile(r"\b(?:remember|note (?:that|down)|jot (?:that |this |it )?down|keep (?:that|this|it) in mind|"
                      r"don'?t forget|for the record)\b", re.I), ("kb_remember",)),)
# "remind me what I'm called" asks what the notes say, not for a reminder: the timer and reminder tools stay out
RECALL = re.compile(r"\bremind me (?:what|who|whose|where|which|how)\b", re.I)
# Experts only when the user names them (the system prompt's rule): a 2B model otherwise sends anything it
# can't do to Claude, which has no browsing or notes either.
NAMED = {"ask_claude": re.compile(r"\bclaude\b", re.I), "ask_opencode": re.compile(r"\bopen ?code\b", re.I),
         # background jobs: only when the user asks for a job or for work in the background
         "start_job": re.compile(r"\b(?:background|long[- ]running|long)[- ](?:job|task)s?\b|\bin the background\b|"
                                 r"\bas a (?:background )?job\b|\b(?:start|run|kick off|launch|create|make|do) (?:a |an |the |"
                                 r"this |that |it )?(?:new )?(?:background )?job\b|^\W*job\b", re.I),
         "job_status": re.compile(r"\bjobs?\b", re.I),
         "cancel_job": re.compile(r"\bjobs?\b", re.I)}


@dataclass
class Ranking:
    """What selection found: the tools to offer, and how sure it is that the request wants one of them."""
    tools: list[Tool]
    scores: dict[str, float] = field(default_factory=dict)  # internal name -> best similarity

    @property
    def best(self) -> tuple[str, float]:
        if not self.scores:
            return "", 0.0
        name = max(self.scores, key=self.scores.get)
        return name, self.scores[name]


class ToolSelector:
    def __init__(self, embedder: LLMClient, model: str, k: int = 6, core: tuple[str, ...] = CORE) -> None:
        self._embedder, self._model, self.k, self.core = embedder, model, k, core
        self._vectors: dict[str, list[float]] = {}  # embedded text -> unit vector
        self._queries: dict[str, list[float]] = {}  # recent query vectors: voice commands repeat

    async def select(self, tools: list[Tool], messages: list[dict]) -> list[Tool]:
        return (await self.rank(tools, messages)).tools

    async def rank(self, tools: list[Tool], messages: list[dict]) -> Ranking:
        """The core tools plus the k tools closest to the latest user turns (and their families),
        in registry order, with every tool's score."""
        query = _query(messages)
        if not query:
            return Ranking(tools)
        try:
            await self._embed([t for tool in tools for t in _texts(tool)])
            q = self._queries.get(query)
            if q is None:
                q = _unit((await self._embedder.embed(["search_query: " + query], self._model))[0])
                if len(self._queries) >= 256:
                    self._queries.pop(next(iter(self._queries)))
                self._queries[query] = q
        except (LLMError, IndexError, KeyError) as e:
            log.warning("tool selection unavailable, offering every tool: %s", e)
            return Ranking(tools)
        scores = {t.name: max(_dot(q, self._vectors[x]) for x in _texts(t)) for t in tools}
        said = _latest(messages)
        tools = [t for t in tools if t.name not in NAMED or NAMED[t.name].search(said)]
        wanted = {n for rx, names in WANTS if rx.search(said) for n in names} | \
                 {n for n, rx in NAMED.items() if rx.search(said)}
        if RECALL.search(said):
            tools = [t for t in tools if family_of(t.name) != "timers"]
            scores = {n: v for n, v in scores.items() if family_of(n) != "timers"}  # not a fit to weigh either
            wanted |= {"kb_search"}
        if self.k <= 0 or len(tools) <= self.k + len(self.core):
            return Ranking(tools, scores)
        ranked = sorted((t for t in tools if t.name not in self.core), key=lambda t: -scores[t.name])
        keep = {t.name for t in ranked[:self.k]} | set(self.core) | wanted
        best = ranked[0].name if ranked else ""
        if fam := family_of(best):  # the best tool's siblings: set / list / cancel a timer
            keep |= {t.name for t in tools if family_of(t.name) == fam}
        return Ranking([t for t in tools if t.name in keep], scores)

    async def _embed(self, texts: list[str]) -> None:
        todo = list(dict.fromkeys(x for x in texts if x not in self._vectors))
        for i in range(0, len(todo), 64):
            chunk = todo[i:i + 64]
            vecs = await self._embedder.embed(chunk, self._model)
            for text, v in zip(chunk, vecs):
                self._vectors[text] = _unit(v)


def _texts(tool: Tool) -> list[str]:
    """What a tool is matched on: its name and description, and each example request on its card."""
    texts = [f"search_document: {tool.model_name.replace('_', ' ')}: {tool.description}"]
    if c := card(tool.name):
        texts += [f"search_document: {e}" for e in c.examples]
    return texts


def _query(messages: list[dict]) -> str:
    """The last user turn, plus the one before so short follow-ups ("yes, send it") keep their topic."""
    said = [m["content"].split("\n\n(Now: ")[0] for m in messages  # without the per-turn time line
            if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if not said:
        return ""
    return (said[-2][:300] + "\n" if len(said) > 1 else "") + said[-1][:1000]


def _latest(messages: list[dict]) -> str:
    said = [m["content"].split("\n\n(Now: ")[0] for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return said[-1] if said else ""


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
