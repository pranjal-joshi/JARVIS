"""Fixes from the traces of 3 Nov 2026: claims of any kind checked against the tools that ran, "remember it"
saving what came before, parts passing results on, clock times worked out by code, reminders, moving notes,
web searches asked for by name, empty replies, and switching back after a rate limit."""

import asyncio
import json
import time
from datetime import datetime

from jarvis_hub.agent.claims import claimed_kind
from jarvis_hub.agent.plan import split_steps
from jarvis_hub.agent.tool_agent import BARE_SAVE, ToolAgent
from jarvis_hub.approval.gate import ApprovalGate, Decision, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import EventBus
from jarvis_hub.kb.graph import ObsidianVault
from jarvis_hub.kb.tools import register_kb_tools
from jarvis_hub.mcp.registry import Tool, ToolRegistry
from jarvis_hub.tools.reminders import Reminders, next_time, register_reminder_tools
from jarvis_hub.tools.when import clock, spoken, when


class ScriptLLM:
    model = "jarvis-2b-gpu"

    def __init__(self, chat, json_out=()):
        self.chats, self.jsons, self.seen, self.schemas, self.tools = list(chat), list(json_out), [], [], []

    async def chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        self.tools.append([t["function"]["name"] for t in tools or []])
        return self.chats.pop(0)

    async def json(self, messages, schema):
        self.schemas.append(schema)
        return self.jsons.pop(0) if self.jsons else {"parts": []}


def say(text):
    return {"role": "assistant", "content": text}


def call(tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}


class YesGate(ApprovalGate):
    """Approves at once, as the user would in the HUD, and keeps what was asked."""

    def __init__(self):
        super().__init__(EventBus(), 2)
        self.asked = []

    async def request(self, **kw):
        self.asked.append(kw)
        return Decision(True, "web")


def setup(llm, *extra):
    ran, reg = [], ToolRegistry()

    def tool(name, params, risk=Risk.READ, reply=None, **kw):
        async def fn(args, ctx):
            ran.append((name, args))
            return reply(args) if callable(reply) else reply or f"{name} ok"
        reg.add(Tool(name, "builtin", name, {"type": "object", "properties": params,
                                             "required": list(params)[:1]}, risk, fn, **kw))

    s = {"type": "string"}
    tool("kb_search", {"query": s}, reply="No matching notes.")
    tool("kb_remember", {"fact": s, "topic": s}, Risk.WRITE, lambda a: f'Saved "{a["fact"]}".', self_scoped=True)
    tool("kb_journal", {"text": s}, Risk.WRITE, self_scoped=True)
    tool("kb_append_to_page", {"page": s, "text": s}, Risk.WRITE)
    tool("set_timer", {"minutes": {"type": "number"}, "at": s, "label": s}, Risk.WRITE,
         lambda a: "Timer set; it goes off tomorrow at 10:00, sir.", final_answer=True)
    tool("notify", {"text": s}, Risk.WRITE)
    tool("web_search", {"query": s}, reply="1. Atomberg lock integration https://github.com/x/atomberg")
    tool("fetch_page", {"url": s}, reply="URL: https://dreadedlama.com/a.html\nThe Atomberg SL1 lock speaks BLE.")
    for name in extra:
        tool(name, {"text": s}, Risk.WRITE)
    a = ToolAgent(llm, reg, YesGate(), AuditStore(":memory:"), ask_for=frozenset())
    return a, ran


def ask(a, text, history=(), session="web-s1"):
    msgs = [*history, {"role": "user", "content": text}]
    return asyncio.run(a.run(messages=msgs, source=Source.WEB, request_id=f"r{time.time_ns()}", bus=EventBus(),
                             trace_session=session))


# ---------- claims of any kind ----------
def test_claims_are_checked_against_the_kind_of_tool_that_ran():
    assert claimed_kind("That's done, sir. I have set a timer for 10 AM tomorrow.", set()) == "timer"
    assert claimed_kind("Understood, sir. Let me save that.", set()) == "notes"
    assert claimed_kind("I've set a timer.", {"message"}) == "timer"  # a notification doesn't back a timer
    assert claimed_kind("Your reminder is set for 10.", set()) == "timer"
    assert claimed_kind("I've sent the notification, sir.", {"message"}) is None
    assert claimed_kind("I've paused the music.", {"device"}) is None
    assert claimed_kind("Shall I set a timer?", set()) is None  # an offer
    assert claimed_kind("Let me recall what you said.", set()) is None
    assert claimed_kind("I couldn't find that in your notes.", set()) is None


def test_a_promise_to_save_becomes_a_pick_from_the_note_writes_only():
    # the capture step finds nothing it can keep, so the model answers, promises and is held to it
    llm = ScriptLLM([say("Understood, sir. Let me save that."), say("Saved, sir.")],
                    [{"facts": []}, {"fact": "configure ESP Home modules in the new home"}])
    a, ran = setup(llm)
    reply = ask(a, "Remember that we need to configure ESP Home modules in the new home")
    assert ran[-1] == ("kb_remember", {"fact": "configure ESP Home modules in the new home"})
    assert "fact" in llm.schemas[1]["properties"] and "tool" not in llm.schemas[1]["properties"]  # forced, no pick
    assert "Saved" in reply or "configure" in reply



def test_remember_that_is_saved_by_the_capture_step_without_the_model():
    fact = "The user needs to configure ESP Home modules in the new home."
    llm = ScriptLLM([], [{"facts": [{"fact": fact, "topic": "home"}]}])
    a, ran = setup(llm)
    reply = ask(a, "Also remember that we need to configure ESP Home modules in the new home")
    assert ran == [("kb_remember", {"fact": fact, "topic": "home"})] and reply.startswith("Noted, sir")
    from jarvis_hub.agent.tool_agent import REMEMBER_THAT
    assert not REMEMBER_THAT.match("remember to call mum at 6") and not REMEMBER_THAT.match("do you remember that")
    assert REMEMBER_THAT.match("note: the gate code changed")

def test_a_timer_claim_with_no_timer_picks_a_timer_tool_or_says_it_isnt_set():
    llm = ScriptLLM([say("That's done, sir. I have set a timer for 10 AM tomorrow.")], [{"tool": "none"}])
    a, ran = setup(llm)
    assert ask(a, "Ok, do it for tomorrow for now") == "I haven't actually set a timer or reminder, sir."
    assert ran == [] and llm.schemas[0]["properties"]["tool"]["enum"] == ["set_timer", "none"]


# ---------- "remember it" saves what came before ----------
def test_bare_save_matches_only_saves_with_nothing_of_their_own():
    for said in ("remember it", "save it to ntoes", "remember what you got earlier", "add the link", "ok save it",
                 "save that in my notes please"):
        assert BARE_SAVE.match(said), said
    for said in ("note that the Atomberg lock is REA", "remember my wife is Sanika", "add milk to groceries"):
        assert not BARE_SAVE.match(said), said


def test_remember_it_saves_the_page_read_last_turn_with_its_link():
    llm = ScriptLLM([call("read_web_page", url="https://dreadedlama.com/a.html"), say("It speaks BLE, sir."),
                     call("remember", fact="The Atomberg SL1 lock speaks BLE."), say("Saved, sir.")])
    a, ran = setup(llm)
    ask(a, "https://dreadedlama.com/a.html")
    history = [{"role": "user", "content": "https://dreadedlama.com/a.html"},
               {"role": "assistant", "content": "It speaks BLE, sir."}]
    ask(a, "remember it", history)
    assert llm.tools[-1] == ["remember", "add_to_journal", "add_to_page"] or \
        set(llm.tools[-1]) == {"kb_remember", "kb_journal", "kb_append_to_page"}
    assert "speaks BLE" in llm.seen[-1][-1]["content"]  # what came before is in front of the model
    saved = [args for name, args in ran if name == "kb_remember"]
    assert saved and "https://dreadedlama.com/a.html" in saved[0]["fact"]  # the link is kept
    # the page's words become a memory only with the user's yes (guard/hooks.py taint)
    assert [x["tool"] for x in a.gate.asked] == ["kb_remember"]


def test_a_note_about_a_link_the_user_gives_keeps_the_link():
    llm = ScriptLLM([call("remember", fact="The article is about the Atomberg lock."), say("Saved, sir.")])
    a, ran = setup(llm)
    ask(a, "Remember this article about the Atomberg lock https://dreadedlama.com/a.html")
    assert ran[0][1]["fact"].endswith("(https://dreadedlama.com/a.html)")


# ---------- parts ----------
def test_a_sentence_that_goes_on_after_then_is_not_split():
    said = ("Also remember that we need a nightly job which looks at the daily conversations and then we can run "
            "it by the cloud session")
    assert split_steps(said) == [said]
    assert split_steps("Do a web search for Frida first, then save it to notes") == \
        ["Do a web search for Frida first", "save it to notes"]
    assert split_steps("what is the weather and what is the time") == ["what is the weather", "what is the time"]


def test_a_later_part_works_from_what_an_earlier_part_found():
    llm = ScriptLLM([call("web_search", query="Frida vs REA"), say("Frida hooks at runtime, sir."),
                     call("remember", fact="Atomberg lock integration: https://github.com/x/atomberg"),
                     say("Saved, sir.")])
    a, ran = setup(llm)
    ask(a, "Do a web search for Frida vs REA first, then save it to notes")
    assert [name for name, _ in ran] == ["web_search", "kb_remember"]
    second = llm.seen[2]  # the save part's conversation
    assert any(m.get("role") == "tool" and "Atomberg lock integration" in m["content"] for m in second)
    assert set(llm.tools[2]) <= {"remember", "add_to_journal", "add_to_page", "kb_remember", "kb_journal",
                                 "kb_append_to_page"}


# ---------- clock times ----------
def test_clock_times_and_days_are_worked_out_by_code():
    now = datetime(2026, 11, 3, 18, 24)
    assert when("10 am tomorrow", now=now) == datetime(2026, 11, 4, 10, 0)
    assert when("10", now=now) == datetime(2026, 11, 4, 10, 0)  # past today: tomorrow
    assert when("6:30 pm", now=now) == datetime(2026, 11, 3, 18, 30)
    assert when("noon", "friday", now) == datetime(2026, 11, 6, 12, 0)
    assert spoken(datetime(2026, 11, 4, 10, 0), now) == "tomorrow at 10:00"
    assert clock("version 3.10") is None and clock("set a timer for 10 minutes") is None


def test_a_timer_for_a_clock_time_is_set_at_that_time_not_as_minutes():
    llm = ScriptLLM([call("set_timer", minutes=720, label="Acrylic follow up")])
    a, ran = setup(llm)
    reply = ask(a, "set_timer for tomorrow 10 AM to get Acrylic follow up from interior")
    assert ran == [("set_timer", {"label": "Acrylic follow up", "at": "10:00 tomorrow"})]
    assert reply == "Timer set; it goes off tomorrow at 10:00, sir."  # the tool's words, not the model's


# ---------- reminders ----------
def test_reminders_repeat_and_one_offs_go_once(tmp_path):
    shown = []

    class Notifier:
        async def send(self, title, text, targets, url="", origin=""):
            shown.append((text, tuple(targets)))

    r = Reminders(tmp_path / "reminders.json", Notifier(), EventBus())
    daily = r.add("Follow up for Acrylic", datetime(2026, 11, 4, 10, 0), "daily", ["android", "hud"])
    r.add("Call the bank", datetime(2026, 11, 4, 9, 0), "once", ["hud"])
    fired = asyncio.run(r.check(datetime(2026, 11, 4, 10, 0, 30)))
    assert [x["text"] for x in fired] == ["Call the bank", "Follow up for Acrylic"]
    assert shown[1] == ("Follow up for Acrylic", ("android", "hud"))
    assert [x["text"] for x in r.all()] == ["Follow up for Acrylic"]  # the one-off is gone
    assert datetime.fromtimestamp(daily["next"]) == datetime(2026, 11, 5, 10, 0)
    assert Reminders(tmp_path / "reminders.json", None, EventBus()).all()[0]["text"] == "Follow up for Acrylic"
    assert next_time({"time": "09:00", "repeat": "weekdays"}, datetime(2026, 11, 6, 10, 0)) == datetime(2026, 11, 9, 9, 0)


def test_a_daily_reminder_from_the_words_the_user_said(tmp_path):
    llm = ScriptLLM([call("set_reminder", text="Follow up for Acrylic and Electrician", time="10", to="phone, hud")])
    a, ran = setup(llm)
    reminders = Reminders(tmp_path / "r.json", None, EventBus())
    register_reminder_tools(a.tools, reminders)
    reply = ask(a, 'Remind me on phone and in HUD to "Follow up for Acrylic and Electrician" every morning at 10 AM')
    [r] = reminders.all()
    assert r["repeat"] == "daily" and r["time"] == "10:00" and r["to"] == ["android", "hud"]
    assert reply.startswith("I'll remind you to Follow up for Acrylic and Electrician every day at 10:00, starting")
    assert reply.endswith("on your phone and the HUD, sir.")


# ---------- moving notes ----------
class Index:
    async def sync(self):
        return None


def test_move_note_moves_matching_notes_with_their_children(tmp_path):
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "journals").mkdir()
    (tmp_path / "journals" / "2026-11-03.md").write_text(
        "- configure ESP Home modules as voice satellites #jarvis\n  - kitchen first\n- bought milk\n", encoding="utf-8")
    (tmp_path / "Jarvis Project.md").write_text("- harness work\n", encoding="utf-8")
    g, reg = ObsidianVault(str(tmp_path)), ToolRegistry()
    register_kb_tools(reg, g, Index())
    out = asyncio.run(reg.get("kb_move_item").call({"match": "ESP Home", "to_page": "jarvis project"}, None))
    assert "Moved 1 note" in out and "[[Jarvis Project]]" in out
    assert (tmp_path / "Jarvis Project.md").read_text(encoding="utf-8") == \
        "- harness work\n- configure ESP Home modules as voice satellites #jarvis\n  - kitchen first\n"
    assert (tmp_path / "journals" / "2026-11-03.md").read_text(encoding="utf-8") == "- bought milk\n"
    assert any((tmp_path / ".trash").iterdir())  # the journal as it was is kept
    assert asyncio.run(reg.get("kb_move_item").call({"match": "nothing like this", "to_page": "Work"}, None)) \
        .startswith("ERROR")


# ---------- web searches asked for by name; empty replies ----------
def test_a_search_asked_for_by_name_is_made_when_the_model_asks_for_a_link_instead():
    llm = ScriptLLM([say("Could you share the repository URL, sir?"), say("Found it, sir: x/atomberg on GitHub.")],
                    [{"query": "Atomberg SL1 Home Assistant integration github"}])
    a, ran = setup(llm)
    reply = ask(a, "search home assistant integration for Atomberg SL1 locks on github")
    assert ran == [("web_search", {"query": "Atomberg SL1 Home Assistant integration github"})]
    assert list(llm.schemas[0]["properties"]) == ["query"] and len(llm.schemas) == 1 and "GitHub" in reply


def test_an_empty_reply_after_a_search_is_asked_for_once_more():
    llm = ScriptLLM([call("search_notes", query="ESP Home"), say(""), say("You noted ESP Home modules, sir.")])
    a, _ = setup(llm)
    assert ask(a, "If you find any task about ESP Home, tell me") == "You noted ESP Home modules, sir."
    assert "Your reply was empty" in llm.seen[-1][-1]["content"]


# ---------- rate limits ----------
class LimitedLLM(ScriptLLM):
    limited = True

    async def chat(self, messages, tools=None):
        if self.model == "groq-qwen" and self.limited:
            from jarvis_hub.llm.client import RateLimited
            raise RateLimited(self.model, 0.01)
        return await super().chat(messages, tools)


def test_back_on_the_rate_limited_model_once_its_limit_has_passed():
    llm = LimitedLLM([say("Canberra, sir."), say("Paris, sir.")])
    llm.model = "groq-qwen"
    a, _ = setup(llm)
    saved = []
    a.fallback, a.on_switch = "jarvis-lfm3-gpu", saved.append
    ask(a, "capital of Australia?")
    assert llm.model == "jarvis-lfm3-gpu"
    a._back = (a._back[0], time.time() - 1)  # its limit has passed
    llm.limited = False
    bus = EventBus()
    q = bus.subscribe(Source.WEB)
    asyncio.run(a.run(messages=[{"role": "user", "content": "capital of France?"}], source=Source.WEB,
                      request_id="r2", bus=bus))
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert llm.model == "groq-qwen" and saved == ["jarvis-lfm3-gpu", "groq-qwen"]
    assert any(e.type == "llm_restored" and e.data == {"from": "jarvis-lfm3-gpu", "to": "groq-qwen"} for e in events)


def test_a_cloud_model_gets_a_smaller_context():
    a, _ = setup(ScriptLLM([]))
    a.context_tokens, a.remote_context_tokens = 8192, 4000
    a.llm.remote = True
    assert a._context() == 4000
    a.llm.remote = False
    assert a._context() == 8192


def test_a_summary_of_notes_just_read_is_not_a_fake_save():
    from jarvis_hub.agent.claims import claimed_kind, kind_of
    recap = "Here is a summary of the notes I have on file:\n\n- **Wife:** Sanika\n- You plan to save ESP Home modules."
    assert claimed_kind(recap, {kind_of("search_notes", True)}) is None  # read the notes, then recounted them
    assert claimed_kind(recap, set()) == "notes"  # nothing read: still a claim
    assert claimed_kind("Noted, sir.", {"recap"}) == "notes"  # a read never backs a save


def test_a_recall_question_never_turns_into_a_note_write():
    from types import SimpleNamespace

    from jarvis_hub.agent.tool_agent import _claimed
    saying = "I've noted that you plan to set up ESP Home modules."
    for asked in ("what do you have about esphome", "tell me everything you remembered so far",
                  "summarize all notes you made"):
        assert _claimed(saying, SimpleNamespace(said=asked, done=set())) is None, asked
    assert _claimed("Noted, sir.", SimpleNamespace(said="remember that I parked on level 2", done=set())) == "notes"
