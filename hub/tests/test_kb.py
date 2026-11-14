import asyncio
from datetime import date

import pytest

from conftest import auth
from jarvis_hub.kb.graph import ObsidianVault, journal_title, parse_blocks
from jarvis_hub.kb.index import KBIndex
from jarvis_hub.kb.tools import recent_memory


def test_parse_blocks_links_and_props():
    blocks, props = parse_blocks("p", "title:: Hello\n- one [[A]] #b\n  more\n\t- two #[[C D]]\n")
    assert props == {"title": "Hello"}
    assert [b.text for b in blocks] == ["one [[A]] #b\nmore", "two #[[C D]]"]
    assert blocks[0].links == ["a", "b"] and blocks[1].links == ["c d"]


def test_obsidian_frontmatter_aliases_tasks_and_short_links(graph_dir):
    blocks, props = parse_blocks("p", "---\ntrigger: webhook\nmatch: \"door bell\"\ntags:\n  - a\n  - b\n---\n"
                                      "- see [[jarvis/people|people]] and [[Home#Lights]]\n")
    assert props == {"trigger": "webhook", "match": "door bell", "tags": "a, b"}
    assert blocks[0].links == ["jarvis/people", "home"]
    g = ObsidianVault(graph_dir)
    (graph_dir / "Chores.md").write_text("- [/] Fix the tap 📅 2026-11-20\n- [-] Paint the shed\n- [x] Mow\n",
                                         encoding="utf-8")
    tasks = {t.text: t for t in g.tasks(include_done=True) if t.page == "Chores"}
    assert tasks["Fix the tap"].status == "DOING" and tasks["Fix the tap"].deadline == "2026-11-20"
    assert tasks["Paint the shed"].status == "CANCELLED" and tasks["Mow"].status == "DONE"
    assert g.page("preferences").name == "jarvis/preferences"  # [[preferences]]: the one page with that file name
    assert g.deep_link("jarvis/preferences") == f"obsidian://open?vault={graph_dir.name}&file=jarvis/preferences"
    (graph_dir / ".obsidian" / "x.md").write_text("- not a page\n", encoding="utf-8")
    assert not any(p.name.startswith(".") for p in g.pages())


def test_pages_tasks_and_namespaces(graph_dir):
    g = ObsidianVault(str(graph_dir))
    names = {p.name for p in g.pages()}
    assert {"Home", "Living Room", "jarvis/preferences", "2026-10-31"} <= names
    tasks = {t.text: t for t in g.tasks()}
    assert tasks["Renew car insurance"].scheduled == "2026-11-03"
    assert "Book AC servicing" not in tasks


def test_writes_stay_in_jarvis_namespace(graph_dir):
    g = ObsidianVault(str(graph_dir))
    assert g.write_jarvis_page("notes", "x") == "jarvis/notes"
    assert (graph_dir / "jarvis" / "notes.md").exists()
    with pytest.raises(ValueError):
        g.write_jarvis_page("jarvis/../../evil", "x")
    title = g.append_journal("did a thing #jarvis", date(2026, 11, 1))
    assert title == "2026-11-01"
    assert "did a thing" in (graph_dir / "journals" / "2026-11-01.md").read_text()


def test_keyword_fallback_search(graph_dir):
    g = ObsidianVault(str(graph_dir))
    idx = KBIndex(g, None, "none", "http://127.0.0.1:9")
    stats = asyncio.run(idx.sync())
    assert stats["vector"] is False
    hits = asyncio.run(idx.search("movie night lights"))
    assert hits[0]["page"] == "Living Room"
    graph = idx.link_graph()
    ids = {n["id"] for n in graph["nodes"]}
    assert {"home", "zigbee", "jarvis", "jarvis/preferences"} <= ids


def test_link_graph_hangs_pageless_folders_off_their_parents(graph_dir):
    g = ObsidianVault(str(graph_dir))
    (graph_dir / "jarvis" / "projects" / "airteno").mkdir(parents=True)
    (graph_dir / "jarvis" / "projects" / "airteno" / "overview.md").write_text("- an app\n", encoding="utf-8")
    edges = {(e["source"], e["target"]) for e in KBIndex(g, None, "none", "http://127.0.0.1:9").link_graph()["edges"]}
    # jarvis/projects/airteno has no page, yet it still links up to jarvis/projects and on to jarvis
    assert {("jarvis/projects/airteno", "jarvis/projects/airteno/overview"),
            ("jarvis/projects", "jarvis/projects/airteno"), ("jarvis", "jarvis/projects")} <= edges


def test_memory(graph_dir):
    g = ObsidianVault(str(graph_dir))
    g.write_jarvis_page("jarvis/preferences", "- likes tea", replace=True)
    mem = recent_memory(g)
    assert any(m["page"].startswith("jarvis/") for m in mem)
    assert all(m["link"].startswith("obsidian://open?vault=") for m in mem)


def test_kb_endpoints(client):
    h = auth("web-token")
    assert client.get("/v1/kb/search", params={"q": "focus mode"}, headers=h).json()[0]["page"] == "Study"
    assert client.get("/v1/kb/graph", headers=h).json()["nodes"]
    assert client.get("/v1/kb/memory", headers=h).status_code == 200
    assert journal_title(date(2026, 11, 22)) == "2026-11-22"


def test_one_point_is_deleted_by_its_place_and_only_if_unchanged(graph_dir):
    g = ObsidianVault(graph_dir)
    path = graph_dir / "Ghidra.md"
    path.write_text("tags:: tools\n\n- Ghidra is written in C\n  source:: guessed\n\t- a nested note\n"
                    "- Ghidra is from the NSA\n- \n- Frida hooks functions\n", encoding="utf-8")
    page = g.page("Ghidra")
    first = page.blocks[0]
    assert first.text == "Ghidra is written in C\nsource:: guessed"
    with pytest.raises(ValueError, match="reload"):
        g.delete_block("Ghidra", first.index, "Ghidra is written in Java")  # what the HUD showed is stale
    g.delete_block("Ghidra", first.index, first.text)  # goes with what is nested under it
    assert [b.text for b in g.page("Ghidra").blocks] == ["Ghidra is from the NSA", "Frida hooks functions"]
    last = g.page("Ghidra").blocks[-1]  # numbered past the empty "- " block, as the parser numbers them
    g.delete_block("Ghidra", last.index, last.text)
    assert path.read_text(encoding="utf-8") == "tags:: tools\n\n- Ghidra is from the NSA\n- \n"
    assert len(list((graph_dir / ".trash").iterdir())) == 2  # the page as it was, each time


def test_the_hud_deletes_a_point_and_it_is_audited(client, graph_dir):
    (graph_dir / "Scratch.md").write_text("- keep me\n- delete me\n", encoding="utf-8")
    page = client.get("/v1/kb/page?name=Scratch", headers=auth("web-token")).json()
    i = page["blocks"].index("delete me")
    r = client.post("/v1/kb/block/delete", headers=auth("web-token"),
                    json={"page": "Scratch", "index": page["index"][i], "text": "delete me"})
    assert r.status_code == 200
    assert client.get("/v1/kb/page?name=Scratch", headers=auth("web-token")).json()["blocks"] == ["keep me"]
    assert client.app.state.audit.recent()[0]["tool"] == "kb_block_delete"
    again = client.post("/v1/kb/block/delete", headers=auth("web-token"),
                        json={"page": "Scratch", "index": page["index"][i], "text": "delete me"})
    assert again.status_code == 409  # gone already: the HUD is told to reload


def test_a_point_is_edited_moved_and_added_by_hand(graph_dir):
    g = ObsidianVault(graph_dir)
    (graph_dir / "jarvis" / "people.md").write_text(
        "- Ravi is my manager\n  learned:: [[2026-11-02]]\n  via:: said by you\n\t- since 2024\n- Asha likes tea\n",
        encoding="utf-8")
    first = g.page("jarvis/people").blocks[0]
    with pytest.raises(ValueError, match="reload"):
        g.edit_block("jarvis/people", first.index, "Ravi is my boss", "x")
    g.edit_block("jarvis/people", first.index, first.text, "Ravi was my manager")
    text = (graph_dir / "jarvis" / "people.md").read_text(encoding="utf-8")
    assert text.startswith("- Ravi was my manager\n  learned:: [[2026-11-02]]\n  via:: edited by you\n\t- since 2024\n")
    asha = g.page("jarvis/people").blocks[-1]
    g.add_block("Friends", g.take_block("jarvis/people", asha.index, asha.text))
    assert [b.text for b in g.page("Friends").blocks] == ["Asha likes tea"]
    assert "Asha" not in (graph_dir / "jarvis" / "people.md").read_text(encoding="utf-8")
    g.add_block("Friends", "Meera plays chess")
    assert len(g.page("Friends").blocks) == 2


def test_a_page_renamed_keeps_its_links_and_the_trash_gives_pages_back(graph_dir):
    g = ObsidianVault(graph_dir)
    (graph_dir / "Ghidra.md").write_text("- a decompiler\n", encoding="utf-8")
    (graph_dir / "Tools.md").write_text("- [[Ghidra]] and #Ghidra and [[Ghidra scripts]]\n", encoding="utf-8")
    to, changed = g.rename_page("Ghidra", "Ghidra SRE")
    assert (to, changed) == ("Ghidra SRE", 1) and g.page("Ghidra") is None
    assert (graph_dir / "Tools.md").read_text(encoding="utf-8") == \
        "- [[Ghidra SRE]] and #[[Ghidra SRE]] and [[Ghidra scripts]]\n"
    with pytest.raises(ValueError, match="already"):
        g.rename_page("Ghidra SRE", "Tools")
    g.trash_page("Ghidra SRE")
    day = graph_dir / "journals" / "2026-11-03.md"
    day.write_text("- one\n- two\n", encoding="utf-8")
    g.delete_block("2026-11-03", 1, "two")
    copies = {t["page"]: t for t in g.trash()}
    assert copies["Ghidra SRE"]["exists"] is False and copies["2026-11-03"]["exists"] is True
    assert g.restore(copies["Ghidra SRE"]["id"]) == "Ghidra SRE"
    assert g.restore(copies["2026-11-03"]["id"]) == "2026-11-03"
    assert day.read_text(encoding="utf-8") == "- one\n- two\n"
    assert any(t["page"] == "2026-11-03" for t in g.trash())  # the day as it was before restoring
    with pytest.raises(KeyError):
        g.restore("../pages/Tools.md")


def test_the_reader_shows_props_and_backlinks_and_edits_by_hand(client, graph_dir):
    h = auth("web-token")
    (graph_dir / "jarvis" / "terms.md").write_text(
        "- SLM means small language model\n  learned:: [[2026-11-02]]\n  via:: said by you\n", encoding="utf-8")
    (graph_dir / "Glossary.md").write_text("- see [[jarvis/terms]]\n", encoding="utf-8")
    page = client.get("/v1/kb/page?name=jarvis/terms", headers=h).json()
    assert page["shown"] == ["SLM means small language model"]
    assert page["props"] == [{"learned": "2026-11-02", "via": "said by you"}]
    assert page["backlinks"] == ["Glossary"] and page["updated"] > 0
    r = client.post("/v1/kb/block/edit", headers=h, json={"page": "jarvis/terms", "index": page["index"][0],
                                                          "text": page["blocks"][0], "new": "SLM = small language model"})
    assert r.status_code == 200
    assert client.post("/v1/kb/block/add", headers=h, json={"page": "jarvis/terms", "text": "RAG = retrieval"}).is_success
    page = client.get("/v1/kb/page?name=jarvis/terms", headers=h).json()
    assert page["shown"] == ["SLM = small language model", "RAG = retrieval"]
    r = client.post("/v1/kb/block/move", headers=h, json={"page": "jarvis/terms", "index": page["index"][1],
                                                          "text": page["blocks"][1], "to": "Glossary"})
    assert r.json()["to"] == "Glossary"
    r = client.post("/v1/kb/page/rename", headers=h, json={"page": "Glossary", "digest": "stale", "to": "Terms"})
    assert r.status_code == 409
    digest = client.get("/v1/kb/page?name=Glossary", headers=h).json()["digest"]
    assert client.post("/v1/kb/page/rename", headers=h, json={"page": "Glossary", "digest": digest, "to": "Terms"}).is_success
    trash = client.get("/v1/kb/trash", headers=h).json()
    assert trash and client.post("/v1/kb/trash/restore", headers=h, json={"id": trash[0]["id"]}).is_success
    assert [a["tool"] for a in client.app.state.audit.recent()][:2] == ["kb_trash_restore", "kb_page_rename"]
