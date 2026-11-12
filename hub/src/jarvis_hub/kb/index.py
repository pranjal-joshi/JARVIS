"""Search index over the Obsidian vault: block embeddings in Qdrant plus a link
graph. The markdown files stay the source of truth; this index can be dropped and rebuilt.
If Qdrant or the embedding model is unavailable, search falls back to keywords."""

import asyncio
import logging
import math
import re
import uuid
from collections import Counter

import httpx

from jarvis_hub.kb.graph import Block, ObsidianVault, Page
from jarvis_hub.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)
COLLECTION = "kb_blocks"
UNSEARCHED = {"jarvis/dreams/diary"}  # indexed for the map, but never a search answer
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


RRF_K = 60  # reciprocal-rank fusion constant: rank matters, raw scores from the two lists don't compare


def _fuse(dense: list[dict], lexical: list[dict], limit: int) -> list[dict]:
    """Merge the meaning hits and the word hits by reciprocal rank. A note in both lists rises; each hit keeps
    the score it came with (cosine for meaning, BM25 for words)."""
    fused: dict[tuple[str, str], tuple[float, dict]] = {}
    for hits in (dense, lexical):
        for rank, h in enumerate(hits):
            key = (h["page"], h["text"])
            prev = fused.get(key)
            fused[key] = ((prev[0] if prev else 0.0) + 1 / (RRF_K + rank + 1), prev[1] if prev else h)
    return [h for _, h in sorted(fused.values(), key=lambda x: -x[0])][:limit]


class _BM25:
    """Okapi BM25 over blocks, held in memory (a personal vault is thousands of blocks, not millions)."""
    K1, B = 1.2, 0.75

    def __init__(self, key: tuple, docs: list[tuple[Block, list[str]]]) -> None:
        self.key = key
        self.docs = [(b, Counter(words), len(words)) for b, words in docs]
        self.avg = sum(n for *_, n in self.docs) / len(self.docs) if self.docs else 1.0
        df: Counter = Counter()
        for _, tf, _ in self.docs:
            df.update(tf.keys())
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    def top(self, terms: list[str], limit: int, min_cover: float) -> list[tuple[float, Block]]:
        out = []
        for b, tf, n in self.docs:
            hit = [t for t in terms if t in tf]
            if not hit or len(hit) / len(terms) < min_cover:
                continue
            s = sum(self.idf[t] * tf[t] * (self.K1 + 1) / (tf[t] + self.K1 * (1 - self.B + self.B * n / self.avg))
                    for t in hit)
            out.append((s, b))
        out.sort(key=lambda x: -x[0])
        return out[:limit]


class KBIndex:
    def __init__(self, graph: ObsidianVault, embedder: LLMClient | None, embed_model: str,
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
        self._bm25: _BM25 | None = None
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
        blocks = page.searchable  # bullets and prose sections (prose has a negative index)
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
        """Memory pages the query names first ("family" -> jarvis/family), then notes found by meaning and
        by words, fused. A short note scores low against a one-word query by meaning alone, so "who is in my
        family" found nothing on a page called family; and a name the embedder has not seen ("Ghidra", "AC
        servicing") is found by its words even when a vaguer note is closer by meaning."""
        topical = self._topic_hits(query, limit)
        seen = {(h["page"], h["text"]) for h in topical}
        words = _content_words(query)
        dense = await self._search(query, limit) if self.vector_ready else []
        lexical = self._keyword_search(" ".join(words), limit + 4, min_cover=0.5 if dense or topical else 0.0)
        found = [h for h in _fuse(dense, lexical, limit) if (h["page"], h["text"]) not in seen]
        return (topical + found)[:max(limit, len(topical))]

    def _topic_hits(self, query: str, limit: int) -> list[dict]:
        """A memory page the query names: its blocks that share the most words with the query first."""
        pages = {p.name.lower(): p for p in self.graph.pages()
                 if not p.is_journal and p.name.lower().startswith("jarvis/")}
        terms = set(_content_words(query))
        out = []
        for word in _content_words(query):
            for topic in {word, word.rstrip("s"), *TOPIC_WORDS.get(word, ())}:
                page = pages.pop(f"jarvis/{topic}", None)
                if page:
                    ranked = sorted(page.searchable, key=lambda b: (-len(terms & set(WORD.findall(b.text.lower()))),
                                                                   b.index < 0, abs(b.index)))
                    out += [{"page": page.name, "text": b.text, "score": 1.0} for b in ranked[:limit]]
        return out

    async def _search(self, query: str, limit: int) -> list[dict]:
        """Closest notes by meaning (empty when the vector index is down)."""
        if self.vector_ready:
            try:
                vec = (await self._embed([query], "query"))[0]
                r = await self._qdrant.post(f"/collections/{COLLECTION}/points/query", json={
                    "query": vec, "limit": limit + 4, "with_payload": True})
                r.raise_for_status()
                hits = [{"page": p["payload"]["page"], "text": p["payload"]["text"],
                         "score": round(p["score"], 3)} for p in r.json()["result"]["points"]
                        # the journal's copy of a memory or correction, and the dream diary (a log of what
                        # jarvis/dreams keeps): repeats that pushed "My name is Pranjal" out of the top 6
                        if not p["payload"]["text"].startswith(("Learned: ", "Corrected: "))
                        and p["payload"]["page"] not in UNSEARCHED]
                top = hits[0]["score"] if hits else 0
                return [h for h in hits if h["score"] >= self.min_score and h["score"] >= top - 0.08][:limit]
            except (httpx.HTTPError, LLMError, KeyError) as e:
                log.warning("vector search failed, using keywords: %s", e)
        return []

    def _bm25_corpus(self) -> "_BM25":
        """The word index over every searchable block, rebuilt when a page changes (pages are cached, so this
        is cheap to check)."""
        pages = [p for p in self.graph.pages() if p.name not in UNSEARCHED]
        key = tuple((p.name, p.digest) for p in pages)
        if self._bm25 is None or self._bm25.key != key:
            docs = [(b, WORD.findall(f"{p.name} {b.text}".lower())) for p in pages for b in p.searchable
                    if not b.text.startswith(("Learned: ", "Corrected: "))]
            self._bm25 = _BM25(key, docs)
        return self._bm25

    def _keyword_search(self, query: str, limit: int, min_cover: float = 0.0) -> list[dict]:
        """BM25 over block words. min_cover: the share of the query's distinct words a block must contain,
        so one common word alone ("lights") does not pull in every note that mentions it."""
        terms = list(dict.fromkeys(WORD.findall(query.lower())))
        if not terms:
            return []
        return [{"page": b.page, "text": b.text, "score": round(s, 3)}
                for s, b in self._bm25_corpus().top(terms, limit, min_cover)]

    def link_graph(self) -> dict:
        pages = self.graph.pages()
        names = {p.name.lower(): p for p in pages}
        nodes: dict[str, dict] = {}
        edges = []
        for p in pages:
            key = p.name.lower()
            nodes[key] = {"id": key, "label": p.name, "journal": p.is_journal,
                          "jarvis": key.startswith("jarvis/"), "blocks": len(p.blocks),
                          "updated": int(p.path.stat().st_mtime)}  # seconds: how fresh the page is
            for target in {self.graph.resolve(t, set(names)) for t in p.links}:  # [[people]] -> jarvis/people
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
