"""Retrieval eval: how often notes search (KBIndex.search) finds the right note for a question.

    hub/.venv/Scripts/python scripts/eval/retrieval.py                          # golden set vs data/kb-graph
    hub/.venv/Scripts/python scripts/eval/retrieval.py --vault kb/sample-graph
    hub/.venv/Scripts/python scripts/eval/retrieval.py --golden scripts/eval/retrieval_golden.local.json
    hub/.venv/Scripts/python scripts/eval/retrieval.py --from-traces data/hub-local/traces.db --days 7

The golden set is JSON: [{"q": "question", "expect": ["a substring of the right note", ...]}]. A hit is a search
result whose text contains one of the expect substrings (case-insensitive). Prints recall@1/@3/@6, MRR and the
questions whose note wasn't in the top 6. Qdrant (QDRANT_URL) and the embedder (EMBED_BASE_URL, EMBED_MODEL) are
used when reachable; otherwise the search is by words only. --from-traces lists recent user questions that made
a search_notes call, to extend a golden set (keep private ones in retrieval_golden.local.json, not in git)."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hub" / "src"))

from jarvis_hub.kb.graph import ObsidianVault  # noqa: E402
from jarvis_hub.kb.index import KBIndex  # noqa: E402

KS = (1, 3, 6)


async def evaluate(vault: str, golden: list[dict], qdrant_url: str = "http://127.0.0.1:9", embedder=None,
                   model: str = "m") -> dict:
    """recall@k for k in KS, mrr, and the misses (questions with no hit in the top max(KS))."""
    index = KBIndex(ObsidianVault(vault), embedder, model, qdrant_url)
    try:
        await index.sync()
    except Exception as e:  # Qdrant or the embedder not reachable: words only
        print(f"(vector search unavailable: {type(e).__name__}; keyword only)", file=sys.stderr)
    found, rr, misses = {k: 0 for k in KS}, 0.0, []
    for case in golden:
        hits = await index.search(case["q"], max(KS))
        want = [e.lower() for e in case["expect"]]
        rank = next((i + 1 for i, h in enumerate(hits) if any(e in h["text"].lower() for e in want)), None)
        for k in KS:
            found[k] += bool(rank and rank <= k)
        rr += 1 / rank if rank else 0.0
        if not rank:
            misses.append({"q": case["q"], "expect": case["expect"], "got": [h["text"][:80] for h in hits[:3]]})
    await index.aclose()
    n =max(len(golden), 1)
    return {"n": len(golden), **{f"recall@{k}": found[k] / n for k in KS}, "mrr": rr / n, "misses": misses}


def questions_from_traces(db: str, days: float = 7, limit: int = 50) -> list[str]:
    """Recent user questions that made a search_notes call (not skill runs, evals, dry runs or tainted turns)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("SELECT session, said, data FROM turns WHERE ts >= ? ORDER BY ts DESC",
                        (time.time() - days * 86400,)).fetchall()
    out = []
    for session, said, data in rows:
        if session.startswith(("skill-", "eval")):
            continue
        d = json.loads(data or "{}")
        if d.get("dry_run") or d.get("tainted"):
            continue
        searched = any(c.get("function", {}).get("name") == "search_notes"
                       for loop in d.get("loops", []) for m in loop.get("convo", [])
                       if m.get("role") == "assistant" for c in m.get("tool_calls") or [])
        if searched and said.strip() and said not in out:
            out.append(said.strip())
        if len(out) >= limit:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", default=str(ROOT / "data" / "kb-graph"))
    ap.add_argument("--golden", default=str(Path(__file__).with_name("retrieval_golden.json")))
    ap.add_argument("--from-traces", metavar="TRACES_DB")
    ap.add_argument("--days", type=float, default=7)
    a = ap.parse_args()
    if a.from_traces:
        for q in questions_from_traces(a.from_traces, a.days):
            print(json.dumps({"q": q, "expect": []}))
        return
    embedder = None
    if base := os.environ.get("EMBED_BASE_URL"):
        from jarvis_hub.llm.client import LLMClient
        embedder = LLMClient(base, os.environ.get("LLM_API_KEY", ""), os.environ.get("EMBED_MODEL", "m"))
    golden = json.loads(Path(a.golden).read_text(encoding="utf-8"))
    r = asyncio.run(evaluate(a.vault, golden, os.environ.get("QDRANT_URL", "http://127.0.0.1:9"), embedder,
                             os.environ.get("EMBED_MODEL", "m")))
    print(f"{r['n']} questions  " + "  ".join(f"recall@{k} {r[f'recall@{k}']:.2f}" for k in KS)
          + f"  MRR {r['mrr']:.2f}")
    for m in r["misses"]:
        print(f"MISS {m['q']!r} (expected {m['expect']}) got {m['got']}")


if __name__ == "__main__":
    main()
