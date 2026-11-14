"""Dreaming (kb/dreaming.py): entity pages, the nightly tidy, stale facts, relation conflicts and the inbox."""

import asyncio
from datetime import date, timedelta

from jarvis_hub.kb.dreaming import INBOX, Dreamer
from jarvis_hub.kb.graph import ObsidianVault

TODAY = date(2026, 11, 4)


def _graph(tmp_path, pages: dict[str, str]):
    g = ObsidianVault(str(tmp_path / "kb"))
    (tmp_path / "kb" / "journals").mkdir(parents=True)
    for name, text in pages.items():
        path = tmp_path / "kb" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return g


class FakeLLM:
    model = "fake"

    def __init__(self, out):
        self.out, self.asked = out, []

    async def json(self, messages, schema):
        self.asked.append(messages[-1]["content"])
        return self.out(messages[-1]["content"]) if callable(self.out) else self.out


def _id(prompt: str, key: str) -> str:
    return next(line.split(" ", 1)[0].rstrip(":") for line in prompt.splitlines() if line[:1] in "CE" and key in line)


def _kept(d):
    return {(p["page"], t) for t, p in d.kept()}


def test_an_entity_gets_its_page_and_undo_puts_the_facts_back(tmp_path):
    g = _graph(tmp_path, {"jarvis/tools": (
        "- Z2M runs on the Raspberry Pi.\n  learned:: [[2026-11-01]]\n  via:: said by you\n"
        "- Zigbee2MQTT (Z2M) bridges the Zigbee sensors.\n  learned:: [[2026-11-02]]\n  via:: noted by JARVIS\n"
        "- Z2M is pinned to version 1.40.\n")})  # no via: the user typed it, never moved

    def decide(prompt):
        return {"decisions": [{"op": "entity", "cites": [_id(prompt, "runs on"), _id(prompt, "bridges"), _id(prompt, "pinned")],
                               "entry": "z2m", "text": "Z2M", "type": "project", "aliases": ["Zigbee2MQTT"],
                               "relations": ["part_of homelab"]},
                              {"op": "entity", "cites": [_id(prompt, "runs on")], "text": "Z2M", "aliases": ["Hue"]}]}
    d = Dreamer(g, None, FakeLLM(decide), tmp_path / "dreams.json")
    rep = asyncio.run(d.run(today=TODAY, force=True))
    assert rep.entities == []  # the user's own block was cited: nothing moved
    decide2 = lambda prompt: {"decisions": [  # noqa: E731
        {"op": "entity", "cites": [_id(prompt, "runs on"), _id(prompt, "bridges")], "entry": "z2m", "text": "Z2M",
         "type": "project", "aliases": ["Zigbee2MQTT"], "relations": ["part_of homelab"]}]}
    d = Dreamer(g, None, FakeLLM(decide2), tmp_path / "dreams2.json")
    rep = asyncio.run(d.run(today=TODAY, force=True))
    assert rep.entities == ["Z2M"]
    page = g.page("jarvis/entities/z2m", exact=True)
    assert page.properties["type"] == "project" and page.properties["aliases"] == "Zigbee2MQTT"
    assert page.properties["relations"] == "part_of [[jarvis/entities/homelab]]"
    kept = _kept(d)
    assert ("jarvis/entities/z2m", "Z2M runs on the Raspberry Pi.") in kept
    assert ("jarvis/tools", "Z2M is pinned to version 1.40.") in kept and len(kept) == 3
    d.undo(0)
    assert g.page("jarvis/entities/z2m", exact=True) is None  # made by the change, empty again: gone
    assert {t for p, t in _kept(d) if p == "jarvis/tools"} == {
        "Z2M runs on the Raspberry Pi.", "Zigbee2MQTT (Z2M) bridges the Zigbee sensors.", "Z2M is pinned to version 1.40."}
    d.undo(0)  # redo: the page and its frontmatter are back
    assert g.page("jarvis/entities/z2m", exact=True).properties["aliases"] == "Zigbee2MQTT"


def test_the_tidy_collapses_duplicates_and_spares_the_users_blocks(tmp_path):
    g = _graph(tmp_path, {"jarvis/people": (
        "- My wife's name is Sanika.\n  learned:: [[2026-11-03]]\n  via:: said by you\n"
        "- The user's wife is named Sanika.\n  learned:: [[2026-10-01]]\n  via:: noted by JARVIS\n"
        "- Pi-hole runs on the NAS.\n"
        "- Pi-hole runs on the NAS.\n"
        "- Pi-hole runs on the NAS box.\n  via:: edited by you\n"
        "- The user's son is Aarav.\n  via:: said by you\n")})
    changed = []
    d = Dreamer(g, None, FakeLLM({"decisions": []}), tmp_path / "dreams.json", on_change=lambda: changed.append(1))
    before = d.kept()
    rep = asyncio.run(d.run(today=TODAY, dry_run=True))
    assert len(rep.merged) == 2 and d.kept() == before  # a dry run writes nothing
    rep = asyncio.run(d.run(today=TODAY))
    texts = [t for t, _ in d.kept()]
    assert texts.count("My wife's name is Sanika.") == 1 and "The user's wife is named Sanika." not in texts
    assert next(p for t, p in d.kept() if t.startswith("My wife"))["learned"] == "[[2026-10-01]]"  # the earliest
    assert texts.count("Pi-hole runs on the NAS.") == 1 and "Pi-hole runs on the NAS box." in texts
    assert "The user's son is Aarav." in texts and changed
    for i in range(len(rep.changes)):
        d.undo(i)
    assert sorted(t for t, _ in d.kept()) == sorted(t for t, _ in before)


def test_a_stale_fact_is_asked_about_once_and_yes_refreshes_it(tmp_path):
    g = _graph(tmp_path, {"jarvis/location": (
        "- The user lives in Pune.\n  learned:: [[2025-01-01]]\n  via:: said by you\n"
        "- The user's office is in Baner.\n  learned:: [[2025-02-01]]\n  via:: said by you\n"
        "- The user's gym is in Aundh.\n  learned:: [[2025-03-01]]\n  as_of:: [[2026-10-01]]\n  via:: said by you\n"
        "- The user's car is parked in Wakad.\n  learned:: [[2025-03-01]]\n  via:: said by you\n"),
        "jarvis/people": "- The user's son is Aarav.\n  learned:: [[2020-01-01]]\n  via:: said by you\n"})
    d = Dreamer(g, None, FakeLLM({"decisions": []}), tmp_path / "dreams.json")
    first = asyncio.run(d.run(today=TODAY, force=True)).questions
    assert len(first) == 2 and all(q.startswith("Is this still true:") for q in first)
    second = asyncio.run(d.run(today=TODAY + timedelta(days=1), force=True)).questions
    assert second == ["Is this still true: The user's car is parked in Wakad?"]  # gym: as_of is recent
    assert asyncio.run(d.run(today=TODAY + timedelta(days=2), force=True)).questions == []
    qs = {q["fact"]: q for q in d.open_questions()}
    d.answer(qs["The user lives in Pune."]["id"], "yes")
    assert next(p for t, p in d.kept() if t == "The user lives in Pune.")["as_of"] == f"[[{date.today()}]]"
    d.answer(qs["The user's office is in Baner."]["id"], "text", "The user's office is in Hinjewadi.")
    texts = [t for t, _ in d.kept()]
    assert "The user's office is in Hinjewadi." in texts and "The user's office is in Baner." not in texts
    d.answer(qs["The user's car is parked in Wakad."]["id"], "no")
    assert d.open_questions() == []


def test_two_targets_for_one_place_become_a_question(tmp_path):
    g = _graph(tmp_path, {"jarvis/entities/pi": (
        "---\ntype: device\naliases: [Raspberry Pi]\n"
        "relations: [located_at [[jarvis/entities/office]], located_at [[jarvis/entities/new-home]]]\n---\n"
        "- The Pi runs Home Assistant.\n  learned:: [[2026-11-01]]\n  via:: said by you\n")})
    d = Dreamer(g, None, FakeLLM({"decisions": []}), tmp_path / "dreams.json")
    rep = asyncio.run(d.run(today=TODAY, force=True))
    assert len(rep.questions) == 1 and "new-home" in rep.questions[0] and "office" in rep.questions[0]
    d.answer(d.open_questions()[0]["id"], "b")
    page = g.page("jarvis/entities/pi", exact=True)
    assert page.properties["relations"] == "located_at [[jarvis/entities/new-home]]"
    assert page.properties["aliases"] == "Raspberry Pi" and page.blocks
    assert asyncio.run(d.run(today=TODAY + timedelta(days=1), force=True)).questions == []


def test_inbox_captures_are_candidates_and_leave_the_inbox_only_for_real(tmp_path):
    g = _graph(tmp_path, {INBOX: ("- The user's Zigbee hub is a Sonoff dongle.\n  learned:: [[2026-10-20]]\n"
                                  "  via:: said by you\n- ok\n")})

    def decide(prompt):
        return {"decisions": [{"op": "add", "cites": [_id(prompt, "Sonoff")], "topic": "tools",
                               "text": "The user's Zigbee hub is a Sonoff dongle."}]}
    llm = FakeLLM(decide)
    d = Dreamer(g, None, llm, tmp_path / "dreams.json")
    assert "Sonoff" in d.context()  # rides along before any dream
    rep = asyncio.run(d.run(today=TODAY, dry_run=True))
    assert rep.added and "Sonoff" in llm.asked[0] and rep.inbox == 0
    assert any("Sonoff" in b.text for b in g.page(INBOX).blocks)
    rep = asyncio.run(d.run(today=TODAY))
    assert rep.inbox == 1 and ("jarvis/tools", "The user's Zigbee hub is a Sonoff dongle.") in _kept(d)
    assert not any("Sonoff" in b.text for b in g.page(INBOX).blocks)
