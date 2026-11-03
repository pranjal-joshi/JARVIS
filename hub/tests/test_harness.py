"""The tool-calling harness around a small model: argument fixes, live-name resolvers, looking in the
notes first, the constrained pick when a tool is skipped, the loop guard and fixed replies for actions."""

import asyncio
import json

from jarvis_hub.agent.tool_agent import NOT_DONE, ToolAgent, _keywords
from jarvis_hub.agent.tool_select import ToolSelector
from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.guard.validate import RESOLVERS, best_name, check, coerce, resolver
from jarvis_hub.mcp.registry import Tool, ToolRegistry


class ScriptLLM:
    """chat() and json() each answer from their own script; what each was sent is kept."""
    model = "jarvis-2b-gpu"

    def __init__(self, chat, json_out=()):
        self.chats, self.jsons, self.seen, self.schemas = list(chat), list(json_out), [], []

    async def chat(self, messages, tools=None):
        self.seen.append(messages)
        return self.chats.pop(0)

    async def json(self, messages, schema):
        self.schemas.append(schema)
        return self.jsons.pop(0)


class WordEmbedder:
    """[1, 0] for text about names or notes, [0, 1] for anything else."""

    async def embed(self, texts, model):
        return [[1.0, 0.0] if ("name" in t or "notes" in t) else [0.0, 1.0] for t in texts]


def say(text):
    return {"role": "assistant", "content": text}


def call(tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}


def setup(llm, selector=None):
    ran, reg = [], ToolRegistry()

    def tool(name, params, risk=Risk.READ, desc="", reply=None):
        async def fn(args, ctx):
            ran.append((name, args))
            return reply or f"{name} ok"
        reg.add(Tool(name, "builtin", desc or name, {"type": "object", "properties": params,
                                                     "required": list(params)[:1]}, risk, fn))

    tool("kb_search", {"query": {"type": "string"}}, desc="search the notes", reply="[jarvis/people] My name is Pranjal")
    tool("notify", {"text": {"type": "string"}}, Risk.WRITE, "show a notification")
    tool("win_media", {"key": {"type": "string", "enum": ["play_pause", "next"]}, "times": {"type": "integer"}},
         Risk.WRITE, "media keys")
    tool("get_time", {}, desc="time now")
    tool("kb_correct", {"wrong": {"type": "string"}, "fact": {"type": "string"}}, Risk.WRITE, "fix a memory")
    a = ToolAgent(llm, reg, ApprovalGate(EventBus(), 2), AuditStore(":memory:"), ask_for=frozenset(),
                  selector=selector)
    return a, ran


def ask(a, text):
    return asyncio.run(a.run(messages=[{"role": "user", "content": text}], source=Source.WEB,
                             request_id="r1", bus=EventBus()))


# ---------- arguments ----------
def test_coerce_fixes_what_needs_no_guessing_and_names_the_rest():
    schema = {"properties": {"minutes": {"type": "number"}, "times": {"type": "integer"},
                             "loud": {"type": "boolean"}, "key": {"type": "string", "enum": ["play_pause", "next"]},
                             "label": {"type": "string"}, "note": {"type": "string"}}, "required": ["minutes"]}
    fixed, problems = coerce(schema, {"minutes": "10 minutes", "times": "three", "loud": "yes", "key": "Next",
                                      "label": ' "pasta" ', "note": None})
    assert fixed == {"minutes": 10.0, "times": 3, "loud": True, "key": "next", "label": "pasta"} and not problems
    _, problems = coerce(schema, {"minutes": "soon", "key": "louder"})
    assert problems == ["minutes must be a number, not 'soon'", "key must be one of play_pause, next"]


def test_best_name_only_picks_when_one_fits():
    labels = ["pasta", "tea"]
    assert best_name("the pasta timer", labels) == "pasta"
    assert best_name("Tea", labels) == "tea"
    assert best_name("eggs", labels) is None
    assert best_name("people", ["jarvis/people", "Groceries"]) == "jarvis/people"
    assert best_name("jarvis", ["jarvis/people", "jarvis/family"]) is None  # two fit: ask instead


def test_resolver_corrects_a_live_name_but_never_for_destructive_tools():
    resolver("t_cancel", lambda a: {**a, "label": best_name(a["label"], ["pasta"]) or a["label"]})
    try:
        t = Tool("t_cancel", "builtin", "", {"properties": {"label": {"type": "string"}}}, Risk.WRITE, None)
        assert check(t, {"label": "the pasta timer"}) == ({"label": "pasta"}, "")
        t.risk = Risk.DESTRUCTIVE
        assert check(t, {"label": "the pasta timer"}) == (None, "")
    finally:
        RESOLVERS.pop("t_cancel")


def test_fixed_arguments_reach_the_tool_and_bad_ones_are_audited_as_rejected():
    llm = ScriptLLM([call("media_control", key="Next"), say("ok")])
    a, ran = setup(llm)
    assert ask(a, "next song") == "I've skipped to the next track, sir."  # a fixed reply for a simple action
    assert ran == [("win_media", {"key": "next"})]
    llm = ScriptLLM([call("media_control", key="louder"), say("I couldn't, sir.")])
    a, ran = setup(llm)
    ask(a, "louder")
    assert ran == [] and a.audit.recent()[0]["status"] == "rejected"
    assert "key must be one of" in llm.seen[1][-1]["content"]  # the model is told what is valid


# ---------- making sure the tool call happens ----------
def test_questions_about_the_user_look_in_the_notes_first():
    llm = ScriptLLM([say("You are Pranjal, sir.")])
    a, ran = setup(llm, ToolSelector(WordEmbedder(), "m", k=1))
    assert ask(a, "remind me what my name is?") == "You are Pranjal, sir."
    assert ran[0] == ("kb_search", {"query": "name"})  # searched by keywords, not the whole question
    assert llm.seen[0][-1]["role"] == "tool" and "Pranjal" in llm.seen[0][-1]["content"]
    assert _keywords("remind me what I'm called again") == "name" and _keywords("what do you know about me") == ""
    llm = ScriptLLM([say("Canberra, sir.")])  # not about the user: no search
    a, ran = setup(llm, ToolSelector(WordEmbedder(), "m", k=1))
    ask(a, "what's the capital of Australia?")
    assert ran == []


def test_a_claimed_action_with_no_tool_becomes_a_constrained_pick():
    llm = ScriptLLM([say("I'll set a notification for you, sir.")],
                    [{"tool": "send_notification"}, {"text": "stand up"}])
    a, ran = setup(llm)
    assert ask(a, "ping my laptop saying stand up") == 'I\'ve sent the notification "stand up", sir.'
    assert ran == [("notify", {"text": "stand up"})]
    assert "send_notification" in llm.schemas[0]["properties"]["tool"]["enum"]
    assert llm.schemas[1]["properties"] == {"text": {"type": "string"}}  # the tool's own schema
    # it picks none: the claim never reaches the user
    llm = ScriptLLM([say("I'll set a notification for you, sir.")], [{"tool": "none"}])
    a, ran = setup(llm)
    assert ask(a, "ping my laptop") == NOT_DONE and ran == []


def test_the_same_call_twice_is_stopped():
    llm = ScriptLLM([call("get_time"), call("get_time"), call("get_time")])
    a, ran = setup(llm)
    assert "going in circles" in ask(a, "time?")
    assert ran == [("get_time", {})]


# ---------- correcting a memory ----------
class NoIndex:
    async def sync(self):
        return {}


def test_correct_memory_replaces_the_wrong_fact_and_keeps_the_rest(tmp_path):
    from jarvis_hub.kb.graph import LogseqGraph
    from jarvis_hub.kb.tools import register_kb_tools
    from jarvis_hub.mcp.registry import ToolCtx
    g = LogseqGraph(str(tmp_path))
    for fact in ("The user's wife's name is Tanika.", "Wife's name is Zanika.", "My name is Pranjal"):
        g.write_jarvis_page("jarvis/people", f"{fact}\n  learned:: [[Nov 3rd, 2026]]")
        g.append_journal(f"Learned: {fact} (see [[jarvis/people]]) #jarvis")
    reg = ToolRegistry()
    register_kb_tools(reg, g, NoIndex())
    tool = reg.get("correct_memory")
    ctx = ToolCtx(EventBus(), Source.WEB, "r")
    out = asyncio.run(tool.call({"wrong": "wife's name", "fact": "The user's wife's name is Sanika."}, ctx))
    assert out.startswith("Corrected [[jarvis/people]]") and "Tanika" in out and "Zanika" in out
    texts = [b.text for b in g.page("jarvis/people").blocks]
    assert not any("anika." in t and "Sanika" not in t for t in texts)
    assert any(t.startswith("The user's wife's name is Sanika.") and "corrected by you" in t for t in texts)
    assert any(t.startswith("My name is Pranjal") for t in texts)  # untouched
    assert "no memory contains" in asyncio.run(tool.call({"wrong": "car colour", "fact": "x"}, ctx))


def test_a_correction_is_not_captured_as_a_new_fact():
    llm = ScriptLLM([say("Very good, sir.")])
    a, _ = setup(llm)
    ask(a, "no, my wife's name is Sanika, not Tanika")
    assert "correct_memory" in llm.seen[0][-1]["content"]  # the hint rides on the user turn
