import asyncio

from jarvis_hub.kb.graph import ObsidianVault
from jarvis_hub.kb.index import KBIndex


def _vault(tmp_path):
    ent = tmp_path / "jarvis" / "entities"
    ent.mkdir(parents=True)
    (ent / "zigbee2mqtt.md").write_text(
        "---\ntype: project\naliases: [Z2M, Zigbee2MQTT]\n"
        "relations: [part_of [[jarvis/entities/homelab]], located_at [[jarvis/entities/new-home]]]\n---\n"
        "- wants Z2M monitored and deployed properly\n    learned:: [[2026-11-14]]\n    via:: noted by JARVIS\n",
        encoding="utf-8")
    (ent / "new-home.md").write_text(
        "---\ntype: place\naliases:\n  - Lakeview flat\n  - the new place\n---\n"
        "- moving in December, coordinator goes in the hallway cabinet\n", encoding="utf-8")
    (ent / "homelab.md").write_text("---\ntype: project\n---\n- runs on Proxmox\n", encoding="utf-8")
    (tmp_path / "jarvis" / "home.md").write_text("".join(f"- home note {i}\n" for i in range(8)), encoding="utf-8")
    return KBIndex(ObsidianVault(str(tmp_path)), None, "m", "http://127.0.0.1:9")


def _texts(idx, q):
    return [h["text"] for h in asyncio.run(idx.search(q))]


def test_list_properties_split_whatever_the_form(tmp_path):
    idx = _vault(tmp_path)
    z = idx.graph.page("jarvis/entities/zigbee2mqtt")
    assert z.listprop("aliases") == ["Z2M", "Zigbee2MQTT"]
    assert z.listprop("relations") == ["part_of [[jarvis/entities/homelab]]", "located_at [[jarvis/entities/new-home]]"]
    assert idx.graph.page("jarvis/entities/new-home").listprop("aliases") == ["Lakeview flat", "the new place"]


def test_an_alias_finds_the_entity(tmp_path):
    idx = _vault(tmp_path)
    assert any("Z2M monitored" in t for t in _texts(idx, "what about zigbee"))


def test_an_entity_brings_its_related_pages(tmp_path):
    idx = _vault(tmp_path)
    texts = _texts(idx, "what are our zigbee plans")  # names only Z2M: the home comes through located_at
    z, home = (next(i for i, t in enumerate(texts) if s in t) for s in ("Z2M monitored", "hallway cabinet"))
    assert z < home
    texts = _texts(idx, "what are our plans with zigbee once we move to the new home")
    assert any("Z2M monitored" in t for t in texts) and any("hallway cabinet" in t for t in texts)


def test_a_multi_word_alias_names_the_entity(tmp_path):
    assert any("hallway cabinet" in t for t in _texts(_vault(tmp_path), "when do we get the lakeview flat keys"))


def test_page_and_link_resolve_by_alias(tmp_path):
    idx = _vault(tmp_path)
    assert idx.graph.page("z2m").name == "jarvis/entities/zigbee2mqtt"
    assert idx.graph.page("Lakeview Flat").name == "jarvis/entities/new-home"
    assert idx.graph.page("z2m", exact=True) is None
    names = {p.name.lower() for p in idx.graph.pages()}
    assert ObsidianVault.resolve("z2m", names, idx.graph.entity_aliases()) == "jarvis/entities/zigbee2mqtt"
    edges = idx.link_graph()["edges"]
    assert {"source": "jarvis/entities/zigbee2mqtt", "target": "jarvis/entities/homelab", "label": "part_of"} in edges


def test_entity_aliases_feed_the_other_name_search(tmp_path):
    idx = _vault(tmp_path)
    assert "z2m" in idx.aliases(["zigbee"])


def test_raw_and_inbox_are_never_answers(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "web-page.md").write_text("- kombucha brewing steps\n", encoding="utf-8")
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "inbox.md").write_text("- kombucha brewing idea\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("- kombucha brewing is fun\n", encoding="utf-8")
    idx = KBIndex(ObsidianVault(str(tmp_path)), None, "m", "http://127.0.0.1:9")
    for q in ("kombucha brewing", "inbox kombucha"):
        assert {h["page"] for h in asyncio.run(idx.search(q))} == {"notes"}


def test_what_you_said_beats_an_equal_note_jarvis_made(tmp_path):
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "a.md").write_text("- the boiler pressure is 1.5 bar\n    via:: noted by JARVIS\n",
                                              encoding="utf-8")
    (tmp_path / "jarvis" / "b.md").write_text("- the boiler pressure is 1.5 bar\n    via:: said by you\n",
                                              encoding="utf-8")
    idx = KBIndex(ObsidianVault(str(tmp_path)), None, "m", "http://127.0.0.1:9")
    assert asyncio.run(idx.search("boiler pressure"))[0]["page"] == "jarvis/b"


def test_a_short_word_does_not_match_a_longer_name():
    from jarvis_hub.kb.index import _starts
    assert _starts("zigbee", "zigbee2mqtt") and _starts("plex", "plex2")
    assert not _starts("home", "homelab") and _starts("homel", "homelab")
