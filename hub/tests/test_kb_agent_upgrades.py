"""Knowledge-base agent upgrades: second-person grammar, memory budget per model, look-first cap, raw sources with
provenance, as_of, quick capture, weekday schedules, the decisions topic and the retrieval eval."""

import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

from jarvis_hub.agent.fastpath import FastPaths
from jarvis_hub.agent.tool_agent import KNOWN, ToolAgent
from jarvis_hub.approval.gate import ApprovalGate, Decision, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.automation.skills import DAILY, on_day
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.kb.graph import ObsidianVault
from jarvis_hub.kb.index import KBIndex
from jarvis_hub.kb.tools import TOPICS, as_of_date, memory_topic, register_kb_tools, second_person
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry
from jarvis_hub.persona.jarvis import SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[2]


class Yes(ApprovalGate):
    def __init__(self):
        super().__init__(EventBus(), 1)

    async def request(self, **kw):
        return Decision(True, "web")


class Script:
    """A model that answers from a script and keeps what it was sent."""

    def __init__(self, replies, model="jarvis-2b-gpu", remote=False):
        self.replies, self.model, self.remote, self.sent = list(replies), model, remote, []

    async def chat(self, messages, tools=None, **kw):
        self.sent.append([dict(m) for m in messages])
        return self.replies.pop(0)


def _call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _agent(graph_dir, replies, fast=False, extra=()):
    g = ObsidianVault(str(graph_dir))
    reg = ToolRegistry()
    register_kb_tools(reg, g, KBIndex(g, None, "m", "http://127.0.0.1:9"))
    for tool in extra:
        reg.add(tool)
    llm = Script(replies)
    a = ToolAgent(llm, reg, Yes(), AuditStore(":memory:"), ask_for=frozenset(),
                  fast=FastPaths(g) if fast else None)
    a.vault = g
    return a, g, llm


def _ask(a, text, session="s1"):
    return asyncio.run(a.run(messages=[{"role": "user", "content": text}], source=Source.WEB, request_id="r",
                             bus=EventBus(), trace_session=session))


def test_notes_about_the_user_are_said_back_with_the_verb_agreeing():
    assert second_person("The user wants a quiet desk.") == "You want a quiet desk."
    assert second_person("The user plans to move. The user defines REA as reverse engineering.") == \
        "You plan to move. You define REA as reverse engineering."
    assert second_person("The user has two kids and the user is vegetarian.") == \
        "You have two kids and you are vegetarian."
    assert second_person("The user watches F1 and the user also studies Rust.") == \
        "You watch F1 and you also study Rust."
    assert second_person("The user's son Chiku. The user Pranjal lives in Pune.") == \
        "Your son Chiku. You Pranjal lives in Pune."  # a name after "the user" is not a verb


def test_the_memory_block_is_sized_for_the_model():
    a = ToolAgent(Script([]), ToolRegistry(), Yes(), AuditStore(":memory:"))
    a.memory = lambda limit: f"- limit {limit}"
    assert a._known() == f"\n\n{KNOWN}\n- limit 500"  # a small local model: core facts only
    a.llm.remote = True
    assert a._known().endswith("- limit 1300")
    a.llm.model = "claude-sonnet-4-5"
    assert a._known().endswith("- limit 2500")
    a.memory = lambda: "- no limit taken"  # an older memory source still works
    assert a._known().endswith("- no limit taken")


def test_look_first_hands_over_a_few_hits_and_nothing_when_nothing_is_found(graph_dir):
    (graph_dir / "jarvis" / "projects.md").write_text(
        "".join(f"- Garden project step {i}: plant row {i}\n" for i in range(10)), encoding="utf-8")
    a, g, llm = _agent(graph_dir, [{"role": "assistant", "content": "Ten steps, sir."},
                                   {"role": "assistant", "content": "I don't know, sir."}])
    _ask(a, "what is my garden project?")
    looked = [m for m in llm.sent[0] if m["role"] == "tool"]
    assert len(looked) == 1
    assert sum(ln.startswith("[") for ln in looked[0]["content"].splitlines()) <= 4  # no whole page
    _ask(a, "what is the zorblax quantum fizzbin?", session="s2")
    assert not [m for m in llm.sent[1] if m["role"] == "tool"]  # nothing relevant: nothing injected


def test_a_web_answer_is_kept_verbatim_and_a_fact_saved_from_it_names_it(graph_dir):
    async def web_search(args, ctx):
        return "- Chai Point (chai.example): best masala chai in Pune, open till 11 pm"
    web = Tool("web_search", "web", "Search the web", {"type": "object", "properties": {
        "query": {"type": "string"}}, "required": ["query"]}, Risk.READ, web_search)
    a, g, llm = _agent(graph_dir, [
        _call("web_search", query="best chai in Pune"), {"role": "assistant", "content": "Chai Point, sir."},
        _call("remember", fact="Chai Point has the best masala chai in Pune, open till 11 pm"),
        {"role": "assistant", "content": "Saved, sir."}], extra=[web])
    _ask(a, "search the web for the best chai in Pune")
    raw = [p for p in g.pages() if p.name.startswith("raw/")]
    assert len(raw) == 1 and raw[0].name.endswith("-best-chai-in-pune")
    text = raw[0].path.read_text(encoding="utf-8")
    assert "web_search for 'best chai in Pune'" in text and "open till 11 pm" in text
    _ask(a, "save that")
    assert f"[[{raw[0].name}]]" in llm.sent[-2][-1]["content"]  # the material names the raw page
    saved = (graph_dir / "jarvis" / "preferences.md").read_text(encoding="utf-8") + "".join(
        p.path.read_text(encoding="utf-8") for p in g.pages() if p.name.startswith("jarvis/"))
    assert f"source:: [[{raw[0].name}]]" in saved


def test_remember_keeps_when_a_fact_is_true(graph_dir):
    g = ObsidianVault(str(graph_dir))
    reg = ToolRegistry()
    register_kb_tools(reg, g, KBIndex(g, None, "m", "http://127.0.0.1:9"))
    out = asyncio.run(reg.get("kb_remember").call({"fact": "I worked at BEL", "topic": "work", "as_of": "2018"},
                                                   ToolCtx(EventBus(), Source.WEB, "r")))
    assert out.startswith("Saved")
    assert "as_of:: [[2018-01-01]]" in (graph_dir / "jarvis" / "work.md").read_text(encoding="utf-8")
    today = date(2026, 11, 14)  # a Saturday
    assert as_of_date("next friday", today) == "2026-11-20"
    assert as_of_date("3rd March 2025", today) == "2025-03-03"
    assert as_of_date("March 2025", today) == "2025-03-01"
    assert as_of_date("since 3 March", today) == "2026-03-03"
    assert as_of_date("2025-03-03", today) == "2025-03-03"
    assert as_of_date("some day", today) is None


def test_quick_capture_goes_to_the_inbox_without_the_model(graph_dir):
    a, g, llm = _agent(graph_dir, [], fast=True)
    assert _ask(a, "capture call the plumber about the leak and the tap") == "Captured, sir."
    assert _ask(a, "note: gate code changed") == "Captured, sir."
    assert llm.sent == []
    inbox = (graph_dir / "jarvis" / "inbox.md").read_text(encoding="utf-8")
    assert "call the plumber about the leak and the tap" in inbox and "gate code changed" in inbox
    assert "via:: said by you" in inbox and "learned:: [[" in inbox
    assert a.fast.match("capture the screen") is None or a.fast.match("capture the screen").intent != "capture"
    assert a.fast.match("jot down buy a kettle").steps == [("capture_note", {"text": "buy a kettle"})]
    assert a.tools.get("capture_note").self_scoped


def test_skills_run_on_named_weekdays():
    for spec, days in (("fri 18:00", "fri"), ("mon,wed 07:30", "mon,wed"), ("07:30", None), ("Mon, Fri 9:05", "Mon, Fri")):
        m = DAILY.match(spec)
        assert m and m[1] == days, spec
    assert not DAILY.match("friday 18:00") and not DAILY.match("every 2h")
    friday = datetime(2026, 11, 13, 18, 30)
    assert on_day("fri", friday) and on_day("mon,fri", friday) and on_day(None, friday)
    assert not on_day("mon,wed", friday)


def test_decisions_have_a_memory_page_and_plans_get_challenged():
    assert "decisions" in TOPICS
    assert memory_topic("decision") == memory_topic("decided") == "decisions"
    assert memory_topic("", "I decided to go with Proxmox over Unraid") == "decisions"
    assert "challenge" in SYSTEM_PROMPT and "decisions" in SYSTEM_PROMPT


def test_retrieval_eval_finds_the_sample_notes():
    sys.path.insert(0, str(ROOT / "scripts" / "eval"))
    from retrieval import evaluate
    golden = json.loads((ROOT / "scripts" / "eval" / "retrieval_golden.json").read_text(encoding="utf-8"))
    r = asyncio.run(evaluate(str(ROOT / "kb" / "sample-graph"), golden))
    assert r["n"] == len(golden) and r["recall@6"] >= 0.8, r["misses"]
