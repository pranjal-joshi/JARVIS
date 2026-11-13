"""Where notes go is decided in code, not by the model (2026-11-13 traces: facts scattered over pages a weak model
invented, a named page ignored, a correction saved beside the wrong fact, one fact written four times)."""

import asyncio
from types import SimpleNamespace

from jarvis_hub.agent.tool_agent import NAMED_PAGE, STATES_A_FACT, CORRECTS
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.guard.hooks import once
from jarvis_hub.kb.graph import ObsidianVault
from jarvis_hub.kb.tools import TOPICS, memory_topic, register_kb_tools
from jarvis_hub.mcp.registry import ToolCtx, ToolRegistry


class NoIndex:
    async def sync(self):
        return {}

    async def search(self, query, limit=6):
        return []


def kb(tmp_path, said=""):
    g, reg = ObsidianVault(str(tmp_path)), ToolRegistry()
    register_kb_tools(reg, g, NoIndex())
    ctx = ToolCtx(EventBus(), Source.WEB, "r", said=said)
    return g, (lambda name, **args: asyncio.run(reg.get(name).call(args, ctx)))


def test_a_models_topic_lands_on_one_of_the_fixed_memory_pages():
    for topic, fact in [("remembering", ""), ("employment", ""), ("companies", ""), ("Sanika's former employers", ""),
                        ("save to Sanika page", "Sanika formerly worked as HR"), ("", "User worked at cloudcover"),
                        ("notes", "HUD test checklist"), ("", "My nephew Pranav lives in Bavdhan")]:
        assert memory_topic(topic, fact) in TOPICS, topic
    assert memory_topic("employment") == memory_topic("companies") == "work"
    assert memory_topic("", "My nephew Pranav lives in Bavdhan") == "family"


def test_remember_never_makes_a_new_memory_page(tmp_path):
    g, run = kb(tmp_path)
    run("kb_remember", fact="Started at BEL in 2018.", topic="employment")
    run("kb_remember", fact="Chiku's half-month birthday is today.", topic="remembering")
    assert {p.name for p in g.pages() if p.name.startswith("jarvis/")} <= {f"jarvis/{t}" for t in TOPICS}


def test_add_to_page_makes_a_page_only_when_the_user_named_it(tmp_path):
    g, run = kb(tmp_path, said="what else do you know")
    assert run("kb_append_to_page", page="Sanika Employment", text="HR at Xoriant").startswith("ERROR")
    g, run = kb(tmp_path, said="save this to Sanika page")
    assert "new page" in run("kb_append_to_page", page="Sanika", text="HR at Xoriant")
    assert g.page("Sanika") is not None


def test_the_users_words_name_the_page_and_say_a_fact():
    assert NAMED_PAGE.search("save this to Sanika page")[1] == "Sanika"
    assert not NAMED_PAGE.search("add this to page from where companies were retrieved")
    for said in ("I worked in cloudcover earlier - from 2022 to 2024", "and before that I started in BEL at 2018",
                 "after 2024 I'm in energy exemplar till now"):
        assert STATES_A_FACT.search(said), said
    assert not STATES_A_FACT.search("I'm in the kitchen")
    assert CORRECTS.search("no, update it - I'm at energy exemplar now, since 2024 onwards")


def test_a_reworded_fact_replaces_the_old_copy(tmp_path):
    g, run = kb(tmp_path)
    run("kb_remember", fact="Sanika used to work at companies Xoriant, Bridgenext, and One2N, all based in Pune as HR.",
        topic="people")
    run("kb_remember", fact="Sanika formerly worked as HR at Xoriant, Bridgenext, and One2N, all based in Pune.",
        topic="people")
    assert len(g.page("jarvis/people").blocks) == 1


def test_a_correction_replaces_the_memory_behind_the_wrong_claim(tmp_path):
    g, run = kb(tmp_path)
    run("kb_remember", fact="The user is employed at BEL.", topic="work")
    run("kb_remember", fact="The user works at Energy Exemplar since 2024.", topic="work",
        corrects="You are currently employed at BEL.")
    texts = [b.text.split("\n")[0] for b in g.page("jarvis/work").blocks]
    assert texts == ["The user works at Energy Exemplar since 2024."]


def test_a_correction_keeps_a_note_that_holds_more_and_says_it_may_be_stale(tmp_path):
    g, run = kb(tmp_path)
    run("kb_remember", fact="Work history: cloudcover from 2022 to 2024; BEL from 2018 until now.", topic="work")
    out = run("kb_remember", fact="The user works at Energy Exemplar since 2024.", topic="work",
              corrects="You are currently employed at BEL.")
    assert "out of date" in out and len(g.page("jarvis/work").blocks) == 2


def test_one_fact_is_written_once_a_turn():
    from jarvis_hub.approval.gate import Risk
    from jarvis_hub.mcp.registry import Tool
    journal = Tool("kb_journal", "kb", "", {}, Risk.WRITE, None)
    t = SimpleNamespace(noted=["Work history: started at cloudcover from 2022 to 2024; started at BEL in 2018."])
    assert "Already saved" in once(journal, {"text": "Work history: cloudcover 2022 to 2024; BEL in 2018."}, t).message
    assert once(journal, {"text": "Bought a new kettle."}, t) is None
