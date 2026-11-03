import asyncio
from datetime import date

import pytest

from conftest import auth
from jarvis_hub.kb.graph import LogseqGraph, journal_title, parse_blocks
from jarvis_hub.kb.index import KBIndex
from jarvis_hub.kb.tools import recent_memory


def test_parse_blocks_links_and_props():
    blocks, props = parse_blocks("p", "title:: Hello\n- one [[A]] #b\n  more\n\t- two #[[C D]]\n")
    assert props == {"title": "Hello"}
    assert [b.text for b in blocks] == ["one [[A]] #b\nmore", "two #[[C D]]"]
    assert blocks[0].links == ["a", "b"] and blocks[1].links == ["c d"]


def test_pages_tasks_and_namespaces(graph_dir):
    g = LogseqGraph(str(graph_dir))
    names = {p.name for p in g.pages()}
    assert {"Home", "Living Room", "jarvis/preferences", "Oct 31st, 2026"} <= names
    tasks = {t.text: t for t in g.tasks()}
    assert tasks["Renew car insurance"].scheduled == "2026-11-03"
    assert "Book AC servicing" not in tasks


def test_writes_stay_in_jarvis_namespace(graph_dir):
    g = LogseqGraph(str(graph_dir))
    assert g.write_jarvis_page("notes", "x") == "jarvis/notes"
    assert (graph_dir / "pages" / "jarvis___notes.md").exists()
    with pytest.raises(ValueError):
        g.write_jarvis_page("jarvis/../../evil", "x")
    title = g.append_journal("did a thing #jarvis", date(2026, 11, 1))
    assert title == "Nov 1st, 2026"
    assert "did a thing" in (graph_dir / "journals" / "2026_11_01.md").read_text()


def test_keyword_fallback_search(graph_dir):
    g = LogseqGraph(str(graph_dir))
    idx = KBIndex(g, None, "none", "http://127.0.0.1:9")
    stats = asyncio.run(idx.sync())
    assert stats["vector"] is False
    hits = asyncio.run(idx.search("movie night lights"))
    assert hits[0]["page"] == "Living Room"
    graph = idx.link_graph()
    ids = {n["id"] for n in graph["nodes"]}
    assert {"home", "zigbee", "jarvis", "jarvis/preferences"} <= ids


def test_memory(graph_dir):
    g = LogseqGraph(str(graph_dir))
    g.write_jarvis_page("jarvis/preferences", "- likes tea", replace=True)
    mem = recent_memory(g)
    assert any(m["page"].startswith("jarvis/") for m in mem)
    assert all(m["link"].startswith("logseq://graph/") for m in mem)


def test_kb_endpoints(client):
    h = auth("web-token")
    assert client.get("/v1/kb/search", params={"q": "focus mode"}, headers=h).json()[0]["page"] == "Study"
    assert client.get("/v1/kb/graph", headers=h).json()["nodes"]
    assert client.get("/v1/kb/memory", headers=h).status_code == 200
    assert journal_title(date(2026, 11, 22)) == "Nov 22nd, 2026"
