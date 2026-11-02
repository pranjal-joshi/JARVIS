"""Default agent loop: OpenAI-style function calling over the tool registry,
with every call passing the approval gate and landing in the audit log."""

import asyncio
import difflib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from jarvis_hub.agent.tool_select import ToolSelector
from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.llm.client import LLMClient, LLMError
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry
from jarvis_hub.persona.jarvis import SYSTEM_PROMPT, source_hint
from jarvis_hub.voice.session import PendingTool, Sessions, answer

log = logging.getLogger(__name__)
MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 6000
DEFAULT_ASK_FOR = frozenset({Risk.DESTRUCTIVE, Risk.EXTERNAL})
SLOW_ORIGINS = ("expert",)  # tools that can take minutes: run in the background for satellites
FAKE_TOOL_MARKERS = ("<tool_result", "</tool_result>", "<tool_call", "<function_call", "<function=")
FAKE_TOOL_NUDGE = ("(system) Your last message wrote a tool call or tool result as plain text; no tool ran. "
                   "Call the tool through the tools interface, or answer without claiming any action happened.")


class ToolAgent:
    def __init__(self, llm: LLMClient, tools: ToolRegistry, gate: ApprovalGate,
                 audit: AuditStore, ask_for: frozenset[Risk] = DEFAULT_ASK_FOR,
                 hidden: frozenset[str] = frozenset(), selector: ToolSelector | None = None,
                 sessions: Sessions | None = None,
                 voice_confirm: frozenset[Risk] = frozenset({Risk.EXTERNAL})) -> None:
        self.llm, self.tools, self.gate, self.audit = llm, tools, gate, audit
        self.ask_for = ask_for  # risk levels that wait for a human; the rest run and are audited
        self.hidden = hidden  # internal tool names the model never sees and may not call
        self.selector = selector  # narrows the tools offered to local models; None = offer them all
        self.sessions = sessions  # satellite conversations; None = no spoken confirmations
        self.voice_confirm = voice_confirm  # approval risks a satellite user may confirm by saying yes
        self._tasks: set[asyncio.Task] = set()  # background expert answers

    def visible(self) -> list[Tool]:
        return [t for t in self.tools.all() if t.name not in self.hidden]

    async def offered(self, messages: list[dict]) -> list[Tool]:
        tools = self.visible()
        if self.selector is None or str(getattr(self.llm, "model", "")).startswith("claude-"):
            return tools
        picked = await self.selector.select(tools, messages)
        log.info("offering %d/%d tools: %s", len(picked), len(tools), ", ".join(t.model_name for t in picked))
        return picked

    async def run(self, *, messages: list[dict], source: Source, request_id: str,
                  bus: EventBus, on_text: Callable[[str], None] | None = None,
                  on_event: Callable[[dict], None] | None = None, session: str | None = None) -> str:
        """on_text, if given, receives the reply as it is generated (new text only). The returned
        string is still the full, final reply; a tool-produced answer is never streamed.
        on_event receives voice cues: {"tool": name} when a tool starts, {"say": text} for a line to
        speak at once (an approval to give elsewhere). session names a satellite's conversation."""
        turn = _Turn(source, request_id, bus, on_text, on_event or (lambda e: None), session)
        if session and self.sessions and (p := self.sessions.take(session)):
            yes = answer(_last_user(messages))
            if yes is not None:  # the answer to "Shall I go ahead, sir?"
                return await self._resume(p, yes, turn)
        # Client-supplied system prompts are dropped: the persona and rules are ours.
        convo = [{"role": "system", "content": SYSTEM_PROMPT + "\n" + source_hint(source)
                  + f"\nCurrent local time: {datetime.now():%A %d %B %Y, %H:%M}."}]
        convo += [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
        return await self._loop(convo, turn)

    async def _loop(self, convo: list[dict], t: "_Turn") -> str:
        schemas = [tool.openai_schema() for tool in await self.offered(convo)]
        t.bus.publish(Event("thinking", t.source, t.request_id, {"step": 0}))
        faked = False
        for step in range(MAX_STEPS):
            streamed = []
            try:
                msg = await (self.llm.chat(convo, schemas, on_text=_speakable_stream(
                    lambda x: (streamed.append(x), t.on_text(x))))
                             if t.on_text else self.llm.chat(convo, schemas))
            except LLMError as e:
                log.warning("%s", e)
                return self._reply(t, "My apologies, sir, the language model is not answering.")
            calls = msg.get("tool_calls") or []
            if calls and streamed or (not calls and _fakes_tool_use(_strip_think(msg.get("content") or ""))
                                      and streamed):
                t.on_text(None)  # what streamed was not the answer after all: tell the client to drop it
            if not calls:
                text = _strip_think(msg.get("content") or "").strip()
                if _fakes_tool_use(text):
                    # Small models sometimes write a tool call or result as text instead of calling
                    # the tool. Nothing ran, so never pass that on as if it had: ask once more.
                    log.warning("model wrote tool markup as text: %.200s", text)
                    if faked:
                        return self._reply(t, "My apologies, sir, I couldn't carry that out. Nothing was changed.")
                    faked = True
                    convo.append({"role": "assistant", "content": text})
                    convo.append({"role": "user", "content": FAKE_TOOL_NUDGE})
                    continue
                return self._reply(t, text or "Done, sir.")
            asked = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": [],
                     **({"_claude": msg["_claude"]} if msg.get("_claude") else {})}
            answers, final = [], None
            for call in calls:
                prep = self._prepare(call)
                if not isinstance(prep, str) and self._needs_voice_yes(prep[0], t):
                    # Ask aloud and end the turn; the user's next words answer it (see run/_resume).
                    tool, args = prep
                    asked["tool_calls"].append(call)
                    prompt = f"That will {_describe(tool, args)}. Shall I go ahead, sir?"
                    self.sessions.ask(t.session, PendingTool(
                        prompt, tool.name, args, call.get("id", ""), [*convo, asked, *answers],
                        time.time() + self.sessions.window_s))
                    return self._reply(t, prompt)
                if not isinstance(prep, str) and prep[0].origin in SLOW_ORIGINS and t.source is Source.SATELLITE:
                    # A slow expert answer (up to minutes) must not hold the satellite: run it in the
                    # background and announce it when it lands.
                    tool, args = prep
                    approved_by = await self._approve(tool, args, t)
                    if approved_by.startswith("ERROR"):
                        prep = approved_by
                    else:
                        self._background(tool, args, approved_by, t)
                        return self._reply(t, "Working on it, sir. I'll tell you when the answer is in.")
                result = prep if isinstance(prep, str) else await self._approve_and_run(*prep, t)
                asked["tool_calls"].append(call)
                answers.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
                tool = self.tools.get(call.get("function", {}).get("name", ""))
                body = _unfence(result)
                if tool and tool.final_answer and body is not None and not body.startswith("ERROR"):
                    final = body
            convo += [asked, *answers]
            if final is not None:
                return self._reply(t, final)
            t.bus.publish(Event("thinking", t.source, t.request_id, {"step": step + 1}))
        return self._reply(t, "I seem to be going in circles, sir. Shall we try that differently?")

    async def _resume(self, p: PendingTool, yes: bool, t: "_Turn") -> str:
        tool = self.tools.get(p.tool)
        if tool is None:
            return self._reply(t, "That option has gone, sir. Shall we start again?")
        if not yes:
            self.audit.record(request_id=t.request_id, source=t.source, tool=tool.name, risk=tool.risk,
                              arguments=p.args, status="denied", approved_by=None)
            return self._reply(t, "Very well, sir. I've left it.")
        t.bus.publish(Event("tool_call", t.source, t.request_id,
                            {"tool": tool.name, "origin": tool.origin, "risk": tool.risk, "arguments": p.args}))
        if tool.origin in SLOW_ORIGINS:
            self._background(tool, p.args, "voice", t)
            return self._reply(t, "Right away, sir. I'll tell you when the answer is in.")
        result = await self._run(tool, p.args, "voice", t)
        convo = [*p.convo, {"role": "tool", "tool_call_id": p.call_id, "content": result}]
        return await self._loop(convo, t)

    def _needs_voice_yes(self, tool: Tool, t: "_Turn") -> bool:
        """Satellite only: tools that would wait for a HUD approval at one of the voice_confirm risks
        are confirmed by a spoken yes instead (the rest still need the HUD or phone)."""
        return (t.source is Source.SATELLITE and bool(t.session) and self.sessions is not None
                and not tool.self_scoped and tool.risk in self.ask_for and tool.risk in self.voice_confirm)

    def _background(self, tool: Tool, args: dict, approved_by: str, t: "_Turn") -> None:
        async def work() -> None:
            result = await self._run(tool, args, approved_by, t)
            body = _unfence(result) or result
            text = body if not body.startswith("ERROR") else \
                f"I'm afraid {tool.model_name.replace('_', ' ')} failed, sir. {body[6:].strip()}"
            if self.sessions and t.session:
                self.sessions.record(t.session, "", text)
            t.bus.publish(Event("announce", Source.SATELLITE, t.request_id, {"text": text, "kind": "answer"}))
        task = asyncio.get_running_loop().create_task(work())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _prepare(self, call: dict) -> tuple[Tool, dict] | str:
        """The tool and its arguments, or an ERROR that tells the model what to do instead."""
        fn = call.get("function", {})
        name = fn.get("name", "")
        tool = self.tools.get(name)
        if tool is None:
            return self._unknown_tool(name)
        if tool.name in self.hidden:
            return (f"ERROR: {name} is switched off for you. Tell the user it can only be done by hand; "
                    "don't retry.")
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            return f"ERROR: the arguments were not a JSON object. Call {tool.model_name} again with {_example(tool)}"
        if missing := [k for k in (tool.parameters or {}).get("required", [])
                       if args.get(k) in (None, "")]:
            return (f"ERROR: {tool.model_name} needs {', '.join(missing)}. "
                    f"Call it again with {_example(tool)}, or ask the user for what is missing.")
        return tool, args

    async def _run_tool(self, call: dict, source: Source, request_id: str, bus: EventBus) -> str:
        prep = self._prepare(call)
        if isinstance(prep, str):
            return prep
        return await self._approve_and_run(*prep, _Turn(source, request_id, bus))

    async def _approve_and_run(self, tool: Tool, args: dict, t: "_Turn") -> str:
        approved_by = await self._approve(tool, args, t)
        if approved_by.startswith("ERROR"):
            return approved_by
        return await self._run(tool, args, approved_by, t)

    async def _approve(self, tool: Tool, args: dict, t: "_Turn") -> str:
        """Who let this run ("policy", "web", "phone"...), or an ERROR when nobody did."""
        name = tool.name  # audit, events and approvals use the internal name
        t.bus.publish(Event("tool_call", t.source, t.request_id,
                            {"tool": name, "origin": tool.origin, "risk": tool.risk, "arguments": args}))
        if tool.self_scoped:
            return "policy:self-scoped"
        if tool.risk not in self.ask_for:
            return "policy"
        if t.source is Source.SATELLITE:  # a satellite can't approve: say where to, and read it back
            t.on_event({"say": f"Please approve on your phone or the HUD: {_describe(tool, args)}."})
        decision = await self.gate.request(request_id=t.request_id, source=t.source,
                                           tool=name, risk=tool.risk, arguments=args)
        if decision.approved:
            return decision.by
        status = "timeout" if decision.by == "timeout" else "denied"
        self.audit.record(request_id=t.request_id, source=t.source, tool=name,
                          risk=tool.risk, arguments=args, status=status, approved_by=None)
        t.bus.publish(Event("tool_result", t.source, t.request_id, {"tool": name, "status": status}))
        return f"ERROR: the user did not approve this action ({status}). Do not retry it."

    async def _run(self, tool: Tool, args: dict, approved_by: str, t: "_Turn") -> str:
        name = tool.name
        t.on_event({"tool": tool.model_name})
        try:
            output = await tool.call(args, ToolCtx(t.bus, t.source, t.request_id))
            status = "error" if output.startswith("ERROR") else "ok"
        except Exception as e:
            log.exception("tool %s failed", name)
            output, status = f"ERROR: {e}", "error"
        output = output[:MAX_TOOL_RESULT_CHARS]
        self.audit.record(request_id=t.request_id, source=t.source, tool=name, risk=tool.risk,
                          arguments=args, status=status, approved_by=approved_by,
                          result=output[:2000])
        t.bus.publish(Event("tool_result", t.source, t.request_id,
                            {"tool": name, "status": status, "preview": output[:300]}))
        # Fence results so the model treats them as data, not instructions.
        return f"<tool_result tool=\"{tool.model_name}\">\n{output}\n</tool_result>"

    def _unknown_tool(self, name: str) -> str:
        names = [t.model_name for t in self.visible()]
        near = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(near)}?" if near else " Answer without it, or say you can't do that."
        return f"ERROR: there is no tool called {name!r}.{hint}"

    @staticmethod
    def _reply(t: "_Turn", text: str) -> str:
        t.bus.publish(Event("reply", t.source, t.request_id, {"text": text}))
        return text


@dataclass
class _Turn:
    source: Source
    request_id: str
    bus: EventBus
    on_text: Callable[[str | None], None] | None = None
    on_event: Callable[[dict], None] = lambda e: None
    session: str | None = None


# How a confirmation or approval reads the action back, e.g. "send a text to Chiku saying on my way".
_SAYS = {
    "web_search": lambda a: f"search the web for {a.get('query', 'that')}",
    "fetch_page": lambda a: "read that web page",
    "ask_claude": lambda a: "send your question to Claude",
    "ask_opencode": lambda a: "send your question to opencode",
    "adb_sms_send": lambda a: f"send a text to {a.get('to')} saying {a.get('text')}",
    "adb_whatsapp_send": lambda a: f"send a WhatsApp to {a.get('to')} saying {a.get('text')}",
    "adb_call": lambda a: f"call {a.get('to')}",
}


def _describe(tool: Tool, args: dict) -> str:
    if say := _SAYS.get(tool.name):
        return say(args)
    target = next((str(args[k]) for k in ("name", "page", "to", "query", "app") if args.get(k)), "")
    return tool.model_name.replace("_", " ") + (f" {target}" if target else "")


def _last_user(messages: list[dict]) -> str:
    return next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")


def _example(tool: Tool) -> str:
    """A filled-in argument object for an error message, e.g. {"to": "...", "text": "..."}."""
    props = (tool.parameters or {}).get("properties", {})
    keys = (tool.parameters or {}).get("required") or list(props)[:2]
    sample = {k: (0 if props.get(k, {}).get("type") in ("integer", "number") else "...") for k in keys}
    return json.dumps(sample)


def _unfence(result: str) -> str | None:
    """Tool output without the <tool_result> fence; None if the tool never ran."""
    if not result.startswith("<tool_result"):
        return None
    return result.split("\n", 1)[1].rsplit("\n</tool_result>", 1)[0].strip()


def _fakes_tool_use(text: str) -> bool:
    """True when a reply imitates tool markup instead of using the tools interface."""
    lowered = text.lower()
    return any(m in lowered for m in FAKE_TOOL_MARKERS)


def _speakable_stream(emit: Callable[[str], None]) -> Callable[[str], None]:
    """Wrap a text sink so it only sees reply text: no <think> blocks, nothing once the model starts
    writing tool markup as text, and a trailing "<" held back until it's clearly not a tag."""
    sent, dead = 0, False

    def feed(full: str) -> None:
        nonlocal sent, dead
        if dead:
            return
        if any(m in full for m in FAKE_TOOL_MARKERS):
            dead = True
            return
        if "<think>" in full and "</think>" not in full:
            return
        visible = _strip_think(full).lstrip()
        if (lt := visible.rfind("<")) > -1 and lt >= len(visible) - 16 and ">" not in visible[lt:]:
            visible = visible[:lt]
        if len(visible) > sent:
            emit(visible[sent:])
            sent = len(visible)
    return feed


def _strip_think(text: str) -> str:
    """Drop <think>...</think> blocks some local models emit inline."""
    while "<think>" in text and "</think>" in text:
        start, end = text.index("<think>"), text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    if "</think>" in text:  # opening tag lost (thinking cut short): keep only what follows
        text = text.rsplit("</think>", 1)[1]
    return text
