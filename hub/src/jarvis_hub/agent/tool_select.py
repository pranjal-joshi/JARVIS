"""Per-request tool selection: a small model picks far better from ~9 tools than from 45 (TinyAgent,
RAG-MCP), so local models are offered only the tools closest in meaning to what the user just said,
plus a few that are always useful. A tool is matched by its description and by each example request
on its card (tools/cards.py): its score is the best of those, so one close example is enough. Tools in
the same family come along with the best one. Embeddings use the knowledge base's model; if embedding
fails, every tool is offered as before."""

import logging
import math
from dataclasses import dataclass, field

from jarvis_hub.llm.client import LLMClient, LLMError
from jarvis_hub.mcp.registry import Tool
from jarvis_hub.tools.cards import card, family_of

log = logging.getLogger(__name__)
CORE = ("get_time", "calculate", "kb_search")  # cheap, and often needed alongside anything else


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
        if self.k <= 0 or len(tools) <= self.k + len(self.core):
            return Ranking(tools, scores)
        ranked = sorted((t for t in tools if t.name not in self.core), key=lambda t: -scores[t.name])
        keep = {t.name for t in ranked[:self.k]} | set(self.core)
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


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
