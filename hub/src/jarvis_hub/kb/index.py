"""Search index over the Logseq graph: block embeddings in Qdrant plus a link
graph. Logseq stays the source of truth; this index can be dropped and rebuilt.
If Qdrant or the embedding model is unavailable, search falls back to keywords."""

import asyncio
import logging
import re
import uuid

import httpx

from jarvis_hub.kb.graph import Block, LogseqGraph, Page
from jarvis_hub.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)
COLLECTION = "logseq_blocks"
NS = uuid.UUID("8f5b0c2e-6a0b-4d55-9b5f-6a7c1e2d3f40")
WORD = re.compile(r"[a-z0-9]+")


# words that name a memory page without saying its name ("relatives" -> jarvis/family)
TOPIC_WORDS = {
    "relatives": ("family",), "relative": ("family",), "nephew": ("family",), "niece": ("family",),
    "cousin": ("family",), "son": ("family", "people"), "daughter": ("family", "people"),
    "wife": ("people", "family"), "husband": ("people", "family"), "kids": ("family",), "children": ("family",),
    "birthday": ("dates",), "birthdays": ("dates",), "anniversary": ("dates",), "anniversaries": ("dates",),
    "upcoming": ("events", "dates"), "festival": ("events",), "festivals": ("events",), "plans": ("events",),
    "address": ("location",), "live": ("location",), "home": ("location",), "phone": ("phone",),
    "likes": ("preferences",), "prefer": ("preferences",), "preference": ("preferences",),
}
_FILLER = set("""a an the and or of to in on at for about is are was were be been am do does did have has had i i'm
me my mine you your what what's whats when where who whose which how tell remind again please jarvis sir can could
would will know any anything that this it there here else important all some list show""".split())


def _content_words(query: str) -> list[str]:
    return [w for w in WORD.findall(query.lower()) if w not in _FILLER]


class KBIndex:
    def __init__(self, graph: LogseqGraph, embedder: LLMClient | None, embed_model: str,
                 qdrant_url: str, min_score: float = 0.6) -> None:
        self.graph = graph
        # nomic scores unrelated notes up to ~0.6 on a small graph: below this (or well below the
        # best hit) a note is noise that would leak into the reply
        self.min_score = min_score
        self._embedder = embedder
        self._model = embed_model
        self._qdrant = httpx.AsyncClient(base_url=qdrant_url.rstrip("/"), timeout=30)
        self._digests: dict[str, str] = {}
        self._dim: int | None = None
        self._sync_lock = asyncio.Lock()
        self.vector_ready = False
        self.last_sync: dict = {}

    # ---------- sync ----------
    async def sync(self) -> dict:
        async with self._sync_lock:
            pages = self.graph.pages()
            changed = [p for p in pages if self._digests.get(p.name) != p.digest]
            removed = set(self._digests) - {p.name for p in pages}
            stats = {"pages": len(pages), "changed": len(changed), "removed": len(removed),
                     "vector": False}
            try:
                if changed or removed or not self.vector_ready:
                    await self._ensure_collection()
                    if not self.vector_ready:  # first sync since start: pages deleted while the hub was down
                        stats["removed"] += await self._drop_orphans({p.name for p in pages})
                    for name in removed:
                        await self._delete_page(name)
                        self._digests.pop(name, None)
                    targets = pages if not self.vector_ready else changed
                    for p in targets:
                        await self._index_page(p)
                        self._digests[p.name] = p.digest
                self.vector_ready = True
                stats["vector"] = True
            except (httpx.HTTPError, LLMError, KeyError) as e:
                log.warning("vector index unavailable, keyword search only: %s", e)
                self.vector_ready = False
            self.last_sync = stats
            return stats

    async def _ensure_collection(self) -> None:
        if self._dim is None:
            self._dim = len((await self._embed(["probe"], "document"))[0])
        r = await self._qdrant.get(f"/collections/{COLLECTION}")
        if r.status_code == 200:
            size = r.json()["result"]["config"]["params"]["vectors"]["size"]
            if size == self._dim:
                return
            await self._qdrant.delete(f"/collections/{COLLECTION}")
            self._digests.clear()
        r = await self._qdrant.put(f"/collections/{COLLECTION}", json={
            "vectors": {"size": self._dim, "distance": "Cosine"}})
        r.raise_for_status()
        await self._qdrant.put(f"/collections/{COLLECTION}/index",
                               json={"field_name": "page", "field_schema": "keyword"})

    async def _delete_page(self, name: str) -> None:
        r = await self._qdrant.post(f"/collections/{COLLECTION}/points/delete?wait=true", json={
            "filter": {"must": [{"key": "page", "match": {"value": name}}]}})
        r.raise_for_status()

    async def _drop_orphans(self, names: set[str]) -> int:
        """Delete points of pages no longer in the graph; returns how many pages they belonged to."""
        flt = {"must_not": [{"key": "page", "match": {"any": sorted(names)}}]} if names else {}
        r = await self._qdrant.post(f"/collections/{COLLECTION}/points/scroll", json={
            "filter": flt, "limit": 10000, "with_payload": ["page"], "with_vector": False})
        r.raise_for_status()
        gone = {p["payload"]["page"] for p in r.json()["result"]["points"]}
        for name in gone:
            await self._delete_page(name)
        return len(gone)

    async def _index_page(self, page: Page) -> None:
        await self._delete_page(page.name)
        blocks = page.blocks
        for i in range(0, len(blocks), 32):
            chunk = blocks[i:i + 32]
            vectors = await self._embed([f"{page.name}: {b.text}" for b in chunk], "document")
            points = [{
                "id": str(uuid.uuid5(NS, f"{page.name}#{b.index}")),
                "vector": v,
                "payload": {"page": page.name, "block": b.index, "text": b.text,
                            "links": b.links, "journal": page.is_journal},
            } for b, v in zip(chunk, vectors)]
            r = await self._qdrant.put(f"/collections/{COLLECTION}/points?wait=true",
                                       json={"points": points})
            r.raise_for_status()

    async def _embed(self, texts: list[str], kind: str) -> list[list[float]]:
        if self._embedder is None:
            raise LLMError("no embedder configured")
        # nomic-embed-text expects task prefixes; harmless for other models.
        prefix = "search_query: " if kind == "query" else "search_document: "
        return await self._embedder.embed([prefix + t for t in texts], self._model)

    # ---------- query ----------
    async def search(self, query: str, limit: int = 6) -> list[dict]:
        """Memory pages the query names first ("family" -> jarvis/family), then the closest notes by
        meaning, then (if those found nothing) a plain word match. A short note scores low against a
        one-word query by meaning alone, so "who is in my family" found nothing on a page called family."""
        topical = self._topic_hits(query, limit)
        seen = {(h["page"], h["text"]) for h in topical}
        found = [h for h in await self._search(query, limit) if (h["page"], h["text"]) not in seen]
        if not found and not topical:
            found = self._keyword_search(" ".join(_content_words(query)), limit)
        return (topical + found)[:max(limit, len(topical))]

    def _topic_hits(self, query: str, limit: int) -> list[dict]:
        pages = {p.name.lower(): p for p in self.graph.pages()
                 if not p.is_journal and p.name.lower().startswith("jarvis/")}
        out = []
        for word in _content_words(query):
            for topic in {word, word.rstrip("s"), *TOPIC_WORDS.get(word, ())}:
                page = pages.pop(f"jarvis/{topic}", None)
                if page:
                    out += [{"page": page.name, "text": b.text, "score": 1.0} for b in page.blocks[:limit]]
        return out

    async def _search(self, query: str, limit: int) -> list[dict]:
        if self.vector_ready:
            try:
                vec = (await self._embed([query], "query"))[0]
                r = await self._qdrant.post(f"/collections/{COLLECTION}/points/query", json={
                    "query": vec, "limit": limit + 4, "with_payload": True})
                r.raise_for_status()
                hits = [{"page": p["payload"]["page"], "text": p["payload"]["text"],
                         "score": round(p["score"], 3)} for p in r.json()["result"]["points"]
                        if not p["payload"]["text"].startswith("Learned: ")]  # journal copy of a memory
                top = hits[0]["score"] if hits else 0
                return [h for h in hits if h["score"] >= self.min_score and h["score"] >= top - 0.08][:limit]
            except (httpx.HTTPError, LLMError, KeyError) as e:
                log.warning("vector search failed, using keywords: %s", e)
        return self._keyword_search(" ".join(_content_words(query)), limit)

    def _keyword_search(self, query: str, limit: int) -> list[dict]:
        terms = set(WORD.findall(query.lower()))
        if not terms:
            return []
        scored: list[tuple[float, Block]] = []
        for page in self.graph.pages():
            for b in page.blocks:
                words = set(WORD.findall(f"{page.name} {b.text}".lower()))
                hit = len(terms & words)
                if hit:
                    scored.append((hit / len(terms), b))
        scored.sort(key=lambda s: -s[0])
        return [{"page": b.page, "text": b.text, "score": round(s, 3)} for s, b in scored[:limit]]

    def link_graph(self) -> dict:
        pages = self.graph.pages()
        names = {p.name.lower(): p for p in pages}
        nodes: dict[str, dict] = {}
        edges = []
        for p in pages:
            key = p.name.lower()
            nodes[key] = {"id": key, "label": p.name, "journal": p.is_journal,
                          "jarvis": key.startswith("jarvis/"), "blocks": len(p.blocks)}
            for target in p.links:
                if target not in names and target not in nodes:
                    nodes[target] = {"id": target, "label": target, "journal": False,
                                     "jarvis": target.startswith("jarvis/"), "blocks": 0}
                edges.append({"source": key, "target": target})
            if "/" in key:  # namespace parent -> child
                parent = key.rsplit("/", 1)[0]
                nodes.setdefault(parent, {"id": parent, "label": parent, "journal": False,
                                          "jarvis": parent.startswith("jarvis"), "blocks": 0})
                edges.append({"source": parent, "target": key})
        return {"nodes": list(nodes.values()), "edges": edges}

    async def aclose(self) -> None:
        await self._qdrant.aclose()
