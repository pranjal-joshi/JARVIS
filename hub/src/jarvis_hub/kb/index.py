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

from jarvis_hub.kb.graph import ENTITIES_NS, LINK, PROP, Block, ObsidianVault, Page
from jarvis_hub.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)
COLLECTION = "kb_blocks"
# indexed for the map, but never a search answer: the dream diary, the unsorted inbox, raw/ (verbatim web and
# model output, not facts)
UNSEARCHED = {"jarvis/dreams/diary", "jarvis/inbox"}
NS = uuid.UUID("8f5b0c2e-6a0b-4d55-9b5f-6a7c1e2d3f40")
WORD = re.compile(r"[a-z0-9]+")
TRUSTED = re.compile(r"^via::.*\bby you\b", re.M | re.I)  # said / corrected / answered / edited by you


def _unsearched(name: str) -> bool:
    return name.lower() in UNSEARCHED or name.lower().startswith("raw/")


def _entity_names(p: Page) -> list[str]:
    """What an entity page is called: its slug as words, its title, its aliases."""
    return list(dict.fromkeys([p.name.rsplit("/", 1)[-1].replace("-", " "),
                               *filter(None, [p.properties.get("title")]), *p.listprop("aliases")]))


def _starts(q: str, t: str) -> bool:
    """q is the name word t, or a long enough start of it: "zigbee" -> "zigbee2mqtt", not "home" -> "homelab"."""
    return q == t or t.startswith(q) and (len(q) >= 5 or t[len(q)].isdigit())


def _says(words: list[str], name: list[str]) -> bool:
    """The name's words appear in a row in the query; a query word of 4+ letters may start a name word
    ("zigbee" -> "zigbee2mqtt")."""
    n = len(name)
    return n > 0 and any(all(_starts(q, t) for q, t in zip(words[i:i + n], name))
                         for i in range(len(words) - n + 1))


def _ranked(page: Page, terms: set[str]) -> list[Block]:
    """A page's blocks, those sharing the most words with the query first."""
    return sorted(page.searchable, key=lambda b: (-len(terms & set(WORD.findall(b.text.lower()))),
                                                  b.index < 0, abs(b.index)))


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


TERMS_PAGE = "jarvis/terms"
# "Z2M = Zigbee2MQTT", "REA stands for ...", "defines the abbreviation REA as ..."; the meaning ends at a comma
TERM = re.compile(r"(?:abbreviation\s+)?(?P<a>[\w.+-]{2,20})\s*(?:=|:|stands for|means|as)\s*(?P<z>[^,;.(]{2,60})", re.I)

RRF_K = 60  # reciprocal-rank fusion constant: rank matters, raw scores from the two lists don't compare


def _fuse(*lists: list[dict], limit: int) -> list[dict]:
    """Merge the meaning hits and the word hits by reciprocal rank. A note in both lists rises; each hit keeps
    the score it came with (cosine for meaning, BM25 for words)."""
    fused: dict[tuple[str, str], tuple[float, dict]] = {}
    for hits in lists:
        for rank, h in enumerate(hits):
            key = (h["page"], h["text"])
            prev = fused.get(key)
            fused[key] = ((prev[0] if prev else 0.0) + 1 / (RRF_K + rank + 1), prev[1] if prev else h)
    # a fact you said beats an equal one JARVIS noted: the boost is far below one rank's worth (~2e-4)
    return [h for _, h in sorted(fused.values(), key=lambda x: -(x[0] + 1e-6 * bool(TRUSTED.search(x[1]["text"]))))
            ][:limit]


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
        out.sort(key=lambda x: (-round(x[0], 9), not TRUSTED.search(x[1].text)))
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
            stats = {"pages": len(pages), "changed": 0, "removed": 0, "vector": False}
            try:
                if not self.vector_ready:  # first sync since start (or after a failure)
                    await self._ensure_collection()
                    stats["removed"] += await self._drop_orphans({p.name for p in pages})  # deleted while down
                    if not self._digests:  # what is already indexed, so a restart doesn't re-embed the vault
                        self._digests = await self._stored_digests()
                changed = [p for p in pages if self._digests.get(p.name) != p.digest]
                removed = set(self._digests) - {p.name for p in pages}
                stats["changed"], stats["removed"] = len(changed), stats["removed"] + len(removed)
                if changed or removed:
                    for name in removed:
                        await self._delete_page(name)
                        self._digests.pop(name, None)
                    for p in changed:
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

    async def _stored_digests(self) -> dict[str, str]:
        """page -> digest of the version in Qdrant (points from before digests were stored count as changed)."""
        # shortcut: one scroll of 10,000 points; page through next_page_offset if the vault outgrows it
        r = await self._qdrant.post(f"/collections/{COLLECTION}/points/scroll", json={
            "limit": 10000, "with_payload": ["page", "digest"], "with_vector": False})
        r.raise_for_status()
        return {p["payload"]["page"]: p["payload"]["digest"] for p in r.json()["result"]["points"]
                if p["payload"].get("digest")}

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
        # an entity's notes carry its other names, so a question by any of them is close in meaning
        head = f"{page.name} ({', '.join(_entity_names(page))})" if page.name.lower().startswith(ENTITIES_NS) \
            else page.name
        for i in range(0, len(blocks), 32):
            chunk = blocks[i:i + 32]
            vectors = await self._embed([f"{head}: {b.text}" for b in chunk], "document")
            points = [{
                "id": str(uuid.uuid5(NS, f"{page.name}#{b.index}")),
                "vector": v,
                "payload": {"page": page.name, "digest": page.digest, "block": b.index, "text": b.text,
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
        topical, seen = [], set()
        for h in self._entity_hits(query, limit) + self._topic_hits(query, limit):
            if (key := (h["page"], h["text"])) not in seen:
                seen.add(key)
                topical.append(h)
        words = _content_words(query)
        dense = await self._search(query, limit) if self.vector_ready else []
        lexical = self._keyword_search(" ".join(words), limit + 4, min_cover=0.5 if dense or topical else 0.0)
        # "zigbee" finds the notes that say Z2M, through "Z2M = Zigbee2MQTT" on the terms page
        other = self._keyword_search(" ".join(alias), limit, 0.0) if (alias := self.aliases(words)) else []
        found = [h for h in _fuse(dense, lexical, other, limit=limit) if (h["page"], h["text"]) not in seen]
        # a named page ("new home" -> jarvis/home) keeps half the room: "zigbee plans once we move to the new
        # home" found the Z2M plan, and the home page's six lines pushed it out
        keep = max(limit, len(topical))
        head = topical[:keep - min(len(found), limit // 2)]
        return head + found[:keep - len(head)]

    def aliases(self, words: list[str]) -> list[str]:
        """The other name of each query word the terms page defines ("REA = Reverse Engineer Anything",
        "defines the abbreviation REA as ..."), and each entity page's names (slug, title, aliases), both ways;
        a word matches a name's word or starts it ("zigbee" -> "Zigbee2MQTT")."""
        groups: list[list[list[str]]] = []  # each: the names of one thing, as words
        page = self.graph.page(TERMS_PAGE, exact=True)
        for b in page.blocks if page else []:
            if m := TERM.search(b.text.split("\n")[0]):
                groups.append([WORD.findall(m.group(g).lower()) for g in ("a", "z")])
        groups += [[WORD.findall(n.lower()) for n in _entity_names(p)] for p in self._entities()]
        def hit(w: str, side: list[str]) -> bool:
            return any(_starts(w, t) for t in side)
        out = []
        for names in groups:
            for w in words:
                for i, side in enumerate(names):
                    if hit(w, side):
                        out += [t for j, other in enumerate(names) if j != i for t in other
                                if t not in _FILLER and t not in words and t not in out]
        return out

    def _entities(self) -> list[Page]:
        return [p for p in self.graph.pages() if p.name.lower().startswith(ENTITIES_NS)]

    def _entity_hits(self, query: str, limit: int) -> list[dict]:
        """Entity pages the query names by slug, title or alias: their blocks, then up to 2 blocks from each page
        they relate to (located_at [[jarvis/entities/new-home]])."""
        words, terms = WORD.findall(query.lower()), set(_content_words(query))
        named = [p for p in self._entities() if any(_says(words, WORD.findall(n.lower())) for n in _entity_names(p))]
        own = [{"page": p.name, "text": b.text, "score": 1.0} for p in named for b in _ranked(p, terms)[:limit]]
        related = []
        for p in named:
            for target in (t for rel in p.listprop("relations") for t in LINK.findall(rel)):
                if (q := self.graph.page(target.strip())) and not _unsearched(q.name):
                    related += [{"page": q.name, "text": b.text, "score": 0.8} for b in _ranked(q, terms)[:2]]
        return own + related

    def _topic_hits(self, query: str, limit: int) -> list[dict]:
        """A memory page the query names: its blocks that share the most words with the query first."""
        pages = {p.name.lower(): p for p in self.graph.pages()
                 if not p.is_journal and p.name.lower().startswith("jarvis/") and not _unsearched(p.name)}
        terms = set(_content_words(query))
        out = []
        for word in _content_words(query):
            for topic in {word, word.rstrip("s"), *TOPIC_WORDS.get(word, ())}:
                page = pages.pop(f"jarvis/{topic}", None)
                if page:
                    out += [{"page": page.name, "text": b.text, "score": 1.0} for b in _ranked(page, terms)[:limit]]
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
                        and not _unsearched(p["payload"]["page"])]
                top = hits[0]["score"] if hits else 0
                return [h for h in hits if h["score"] >= self.min_score and h["score"] >= top - 0.08][:limit]
            except (httpx.HTTPError, LLMError, KeyError) as e:
                log.warning("vector search failed, using keywords: %s", e)
        return []

    def _bm25_corpus(self) -> "_BM25":
        """The word index over every searchable block, rebuilt when a page changes (pages are cached, so this
        is cheap to check)."""
        pages = [p for p in self.graph.pages() if not _unsearched(p.name)]
        key = tuple((p.name, p.digest) for p in pages)
        if self._bm25 is None or self._bm25.key != key:
            # without the learned::/via::/as_of::/source:: lines: "source" or a date is not what a note is about
            docs = [(b, WORD.findall(" ".join([p.name] + [ln for ln in b.text.splitlines() if not PROP.match(ln)])
                                     .lower())) for p in pages for b in p.searchable
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
        aliases = self.graph.entity_aliases()
        nodes: dict[str, dict] = {}
        edges = []
        for p in pages:
            key = p.name.lower()
            nodes[key] = {"id": key, "label": p.name, "journal": p.is_journal,
                          "jarvis": key.startswith("jarvis/"), "blocks": len(p.blocks),
                          "updated": int(p.path.stat().st_mtime)}  # seconds: how fresh the page is
            # an entity's relations ("part_of [[jarvis/entities/homelab]]") label their edge with the relation
            kinds = {self.graph.resolve(t.strip().lower(), set(names), aliases): rel.split("[[", 1)[0].strip()
                     for rel in p.listprop("relations") for t in LINK.findall(rel)} \
                if key.startswith(ENTITIES_NS) else {}
            for target in {self.graph.resolve(t, set(names), aliases) for t in p.links}:  # [[people]] -> jarvis/people
                if target not in names and target not in nodes:
                    nodes[target] = {"id": target, "label": target, "journal": False,
                                     "jarvis": target.startswith("jarvis/"), "blocks": 0}
                edges.append({"source": key, "target": target, **({"label": kinds[target]} if kinds.get(target) else {})})
            if "/" in key:  # namespace parent -> child
                parent = key.rsplit("/", 1)[0]
                nodes.setdefault(parent, {"id": parent, "label": parent, "journal": False,
                                          "jarvis": parent.startswith("jarvis"), "blocks": 0})
                edges.append({"source": parent, "target": key})
        return {"nodes": list(nodes.values()), "edges": edges}

    async def aclose(self) -> None:
        await self._qdrant.aclose()
