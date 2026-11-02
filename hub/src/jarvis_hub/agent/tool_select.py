"""Per-request tool selection: a small model picks far better from ~9 tools than from 45 (TinyAgent,
RAG-MCP), so local models are offered only the tools closest in meaning to what the user just said,
plus a few that are always useful. Each tool's name and description is embedded once (with the same
embedding model as the knowledge base); if embedding fails, every tool is offered as before."""

import logging
import math

from jarvis_hub.llm.client import LLMClient, LLMError
from jarvis_hub.mcp.registry import Tool

log = logging.getLogger(__name__)
CORE = ("get_time", "calculate", "kb_search")  # cheap, and often needed alongside anything else


class ToolSelector:
    def __init__(self, embedder: LLMClient, model: str, k: int = 6, core: tuple[str, ...] = CORE) -> None:
        self._embedder, self._model, self.k, self.core = embedder, model, k, core
        self._vectors: dict[tuple[str, str], list[float]] = {}  # (name, description) -> unit vector
        self._queries: dict[str, list[float]] = {}  # recent query vectors: voice commands repeat

    async def select(self, tools: list[Tool], messages: list[dict]) -> list[Tool]:
        """The core tools plus the k tools closest to the latest user turns, in registry order."""
        query = _query(messages)
        if self.k <= 0 or len(tools) <= self.k + len(self.core) or not query:
            return tools
        try:
            await self._embed_tools(tools)
            q = self._queries.get(query)
            if q is None:
                q = _unit((await self._embedder.embed(["search_query: " + query], self._model))[0])
                if len(self._queries) >= 256:
                    self._queries.pop(next(iter(self._queries)))
                self._queries[query] = q
        except (LLMError, IndexError, KeyError) as e:
            log.warning("tool selection unavailable, offering every tool: %s", e)
            return tools
        scored = sorted((t for t in tools if t.name not in self.core),
                        key=lambda t: -_dot(q, self._vectors[(t.name, t.description)]))
        keep = {t.name for t in scored[:self.k]} | set(self.core)
        return [t for t in tools if t.name in keep]

    async def _embed_tools(self, tools: list[Tool]) -> None:
        todo = [t for t in tools if (t.name, t.description) not in self._vectors]
        if not todo:
            return
        vecs = await self._embedder.embed(
            [f"search_document: {t.model_name.replace('_', ' ')}: {t.description}" for t in todo], self._model)
        for t, v in zip(todo, vecs):
            self._vectors[(t.name, t.description)] = _unit(v)


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
