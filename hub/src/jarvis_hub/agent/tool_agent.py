"""Default agent loop: OpenAI-style function calling over the tool registry,
with every call passing the approval gate and landing in the audit log."""

import asyncio
import difflib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from jarvis_hub.agent.fastpath import FastPaths
from jarvis_hub.agent.fastpath import Plan as FastPlan
from jarvis_hub.agent.style import SentenceGate, tidy
from jarvis_hub.agent.tool_select import ToolSelector
from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.guard.hooks import POST_TOOL, PRE_TOOL, audit_text, unsaid
from jarvis_hub.guard.policy import Decision as PolicyDecision
from jarvis_hub.guard.policy import Policy
from jarvis_hub.llm.client import LLMClient, LLMError
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry
from jarvis_hub.persona.jarvis import SYSTEM_PROMPT, source_hint
from jarvis_hub.voice.session import PendingTool, Sessions, answer

log = logging.getLogger(__name__)
MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 6000
REPLY_RESERVE = 600  # tokens kept free for the reply
DEFAULT_ASK_FOR = frozenset({Risk.DESTRUCTIVE, Risk.EXTERNAL})
SLOW_ORIGINS = ("expert",)  # tools that can take minutes: run in the background for satellites
FAKE_TOOL_MARKERS = ("<tool_result", "</tool_result>", "<tool_call", "<function_call", "<function=")
FAKE_TOOL_NUDGE = ("(system) Your last message wrote a tool call or tool result as plain text; no tool ran. "
                   "Call the tool through the tools interface, or answer without claiming any action happened.")
# A reply that says something was done ("Noted, sir.", "I've saved that..."). Checked against the tools
# that actually ran: small models often skip the tool and claim the action anyway.
CLAIMS_ACTION = re.compile(
    r"^\W*(noted|saved|stored|added|sent|done|remembered|logged|recorded|deleted|removed)\b"
    r"|\bI(?:'ve| have|'ll| will)?\s+(?:just\s+|now\s+|also\s+)?(?:saved|noted|stored|remembered|added|logged|"
    r"recorded|written|jotted|made a note|sent|deleted|removed|updated|turned|switched|scheduled)\b"
    r"|\b(?:I'll|I will) (?:remember|keep (?:that|this|it) in mind)", re.I)
CLAIM_NUDGE = ("(system) Your reply says you did something, but no tool that changes anything ran this turn. "
               "If the user wanted it saved or done, call the tool now (remember, for a fact about the user). "
               "Otherwise answer without claiming you did it.")
NOT_DONE = 'I haven\'t actually saved or changed anything, sir. Say "remember ..." and I will.'
# A message that tells JARVIS something about the user without saying "remember".
STATES_A_FACT = re.compile(
    r"\b(?:my|our)\s+(?:[\w']+\s+){0,3}(?:is|are|was|were)\b"
    r"|\bI\s+(?:prefer|like|love|hate|don'?t like|always|never|usually|am allergic|take|drink|eat|live|work"
    r"|was born)\b|\bI'm allergic\b"
    r"|\bkeep (?:that|this|it) in mind\b|\bdon'?t forget\b|\bfor (?:future|later) reference\b|\bfor the record\b",
    re.I)
CAPTURE_PROMPT = ("Extract the facts the user states about themselves, their people, home or preferences, worth "
                  "remembering later. Keep the user's own words, names, dates and numbers; write each as a short "
                  "sentence in the third person (\"The user's sister Chiku's birthday is 9 June.\"). topic: one "
                  "word (people, preferences, home, health, travel, work, dates). No facts: an empty list.")
CAPTURE_SCHEMA = {"type": "object", "required": ["facts"], "properties": {"facts": {"type": "array", "maxItems": 3,
                  "items": {"type": "object", "required": ["fact", "topic"],
                            "properties": {"fact": {"type": "string"}, "topic": {"type": "string"}}}}}}
FACT_HINT = ("\nThe user's last message tells you something about them. If it is worth keeping, call remember "
             "with it (exact names, dates and numbers) before you answer.")


class ToolAgent:
    def __init__(self, llm: LLMClient, tools: ToolRegistry, gate: ApprovalGate,
                 audit: AuditStore, ask_for: frozenset[Risk] = DEFAULT_ASK_FOR,
                 hidden: frozenset[str] = frozenset(), selector: ToolSelector | None = None,
                 sessions: Sessions | None = None,
                 voice_confirm: frozenset[Risk] = frozenset({Risk.EXTERNAL}), context_tokens: int = 8192,
                 fast: FastPaths | None = None, policy: Policy | None = None) -> None:
        self.llm, self.tools, self.gate, self.audit = llm, tools, gate, audit
        # what may run without a human (guard/policy.py); without a file, APPROVAL_RISKS (ask_for) decides
        self.policy = policy or Policy(None, ask_for, ":memory:")
        self._bus: EventBus | None = None  # the hub's bus, for work that starts outside a request
        self.hidden = hidden  # internal tool names the model never sees and may not call
        self.selector = selector  # narrows the tools offered to local models; None = offer them all
        self.sessions = sessions  # satellite conversations; None = no spoken confirmations
        self.voice_confirm = voice_confirm  # approval risks a satellite user may confirm by saying yes
        self._tasks: set[asyncio.Task] = set()  # background expert answers
        self.context_tokens = context_tokens  # the model's window; 0 = don't trim
        self.fast = fast  # requests answered by code, no model call (agent/fastpath.py)

    @property
    def ask_for(self) -> frozenset[Risk]:
        return self.policy.ask_for

    @ask_for.setter
    def ask_for(self, risks: frozenset[Risk]) -> None:
        self.policy.set_ask_for(risks)

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
                  on_event: Callable[[dict], None] | None = None, session: str | None = None,
                  dry_run: bool = False) -> str:
        """on_text, if given, receives the reply as it is generated (new text only). The returned
        string is still the full, final reply; a tool-produced answer is never streamed.
        on_event receives voice cues: {"tool": name} when a tool starts, {"say": text} for a line to
        speak at once (an approval to give elsewhere). session names a satellite's conversation.
        dry_run: tools that change anything or send data out are not run, only recorded (evals, new skills)."""
        turn = _Turn(source, request_id, bus, on_text, on_event or (lambda e: None), session, dry_run=dry_run,
                     model=str(getattr(self.llm, "model", "")))
        if session and self.sessions and (p := self.sessions.take(session)):
            yes = answer(_last_user(messages))
            if yes is not None:  # the answer to "Shall I go ahead, sir?"
                turn.route = "confirm"
                return await self._resume(p, yes, turn)
        # Client-supplied system prompts are dropped: the persona and rules are ours. The system prompt
        # is the same on every turn so Ollama can reuse its cached prefix; what changes (the time, hints)
        # rides on the latest user message instead.
        said = _last_user(messages)
        turn.heard = " ".join(m["content"] for m in messages if m.get("role") == "user")[-1500:]
        if self.fast and (plan := self.fast.match(said)):
            if (reply := await self._fast(plan, turn)) is not None:
                return reply
        states = STATES_A_FACT.search(said) and not said.rstrip().endswith("?")
        if states and not self._is_claude() and (reply := await self._capture(said, turn)):
            return reply
        hint = FACT_HINT if states else ""
        convo = [{"role": "system", "content": SYSTEM_PROMPT + "\n" + source_hint(source)}]
        convo += [dict(m) for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
        for m in reversed(convo):
            if m["role"] == "user":
                m["content"] = f"{m['content']}\n\n(Now: {datetime.now():%A %d %B %Y, %H:%M}.{hint})"
                break
        return await self._loop(convo, turn)

    async def _loop(self, convo: list[dict], t: "_Turn") -> str:
        if t.route != "confirm":
            t.route = "model"
        schemas = [tool.openai_schema() for tool in await self.offered(convo)]
        t.bus.publish(Event("thinking", t.source, t.request_id, {"step": 0}))
        faked = claimed = False
        for step in range(MAX_STEPS):
            streamed = []
            gate = SentenceGate(lambda x: (streamed.append(x), t.on_text(x)), t.source) if t.on_text else None
            _fit(convo, schemas, self.context_tokens)
            try:
                msg = await (self.llm.chat(convo, schemas, on_text=_speakable_stream(gate.feed))
                             if gate else self.llm.chat(convo, schemas))
            except LLMError as e:
                log.warning("%s", e)
                return self._reply(t, "My apologies, sir, the language model is not answering.")
            calls = msg.get("tool_calls") or []
            raw = _strip_think(msg.get("content") or "").strip()
            if gate and not calls and not _fakes_tool_use(raw) and not (CLAIMS_ACTION.search(raw) and not t.changed):
                gate.flush()
            if calls and streamed or (not calls and _fakes_tool_use(raw) and streamed):
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
                if CLAIMS_ACTION.search(text) and not t.changed:
                    # "Noted, sir." with nothing saved: send it back once, then don't pass the claim on.
                    log.warning("reply claims an action no tool did: %.200s", text)
                    if streamed:
                        t.on_text(None)
                    if claimed:
                        return self._reply(t, NOT_DONE)
                    claimed = True
                    convo.append({"role": "assistant", "content": text})
                    convo.append({"role": "user", "content": CLAIM_NUDGE})
                    continue
                if not text:  # nothing to say and nothing done: never answer "Done"
                    return self._reply(t, "Done, sir." if t.changed else "Sorry, sir, I didn't catch that.")
                return self._reply(t, tidy(text, t.source))
            asked = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": [],
                     **({"_claude": msg["_claude"]} if msg.get("_claude") else {})}
            answers, final = [], None
            for call in calls:
                prep, hold = self._prepare(call), ""
                if not isinstance(prep, str):
                    for hook in PRE_TOOL:  # guard/hooks.py: needless search, unsaid names/dates, taint
                        if v := hook(prep[0], prep[1], t):
                            if v.kind == "error":
                                prep = v.message
                                break
                            hold = v.message  # "ask": a human decides, whatever the policy says
                if not isinstance(prep, str) and not hold and self._needs_voice_yes(prep[0], prep[1], t):
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
                    approved_by = await self._approve(tool, args, t, hold)
                    if approved_by.startswith("ERROR"):
                        prep = approved_by
                    else:
                        self._background(tool, args, approved_by, t)
                        return self._reply(t, "Working on it, sir. I'll tell you when the answer is in.")
                result = prep if isinstance(prep, str) else await self._approve_and_run(*prep, t, hold)
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
        # no automatic hand-off to a bigger model: Claude only when the user asks for it
        claude = " Or say \"ask Claude\" and I'll put it to Claude." if any(
            x.name == "ask_claude" for x in self.visible()) else ""
        return self._reply(t, "I seem to be going in circles, sir. Shall we try that differently?" + claude)

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

    async def run_orphan(self, p) -> None:
        """An approval left over from before a restart was approved: run it now and say what happened."""
        tool = self.tools.get(p.tool)
        rid = f"orphan-{p.id[:8]}"
        t = _Turn(p.source, rid, self._bus or EventBus())
        if tool is None or tool.name in self.hidden:
            text = f"I can no longer run {p.tool}, sir; it isn't available."
        else:
            body = _unfence(await self._run(tool, p.arguments, "web:after-restart", t)) or ""
            text = (f"Done, sir: {p.says or tool.model_name}." if not body.startswith("ERROR")
                    else f"That failed, sir: {body[6:].strip()}")
        t.bus.publish(Event("reply", Source.WEB, rid, {"text": text}))
        if p.source is Source.SATELLITE:
            t.bus.publish(Event("announce", Source.SATELLITE, rid, {"text": text, "kind": "approval"}))

    def _is_claude(self) -> bool:
        return str(getattr(self.llm, "model", "")).startswith("claude-")

    async def _capture(self, said: str, t: "_Turn") -> str | None:
        """Memory as its own step: when the user states something about themselves, a schema-constrained
        call pulls the facts out of THEIR words only, each is checked against what they said, and saved
        with "said by you" as its source. A 2B model asked to answer and remember at once mostly answers."""
        tool = self.tools.get("kb_remember")
        if tool is None or tool.name in self.hidden or not hasattr(self.llm, "json"):
            return None
        try:
            out = await self.llm.json([{"role": "system", "content": CAPTURE_PROMPT},
                                       {"role": "user", "content": said}], CAPTURE_SCHEMA)
        except LLMError as e:
            log.info("memory capture unavailable: %s", e)
            return None
        facts = []
        for f in (out or {}).get("facts", [])[:3]:
            fact, topic = str(f.get("fact", "")).strip(), str(f.get("topic", "") or "memory").strip()
            if len(fact) < 4 or unsaid(tool, {"fact": fact}, said, []):
                continue  # empty, or names/dates the user didn't say: leave it to the normal path
            facts.append((fact, re.sub(r"[^a-z]", "", topic.lower())[:20] or "memory"))
        if not facts:
            return None
        t.route = "memory"
        t.bus.publish(Event("thinking", t.source, t.request_id, {"step": 0, "route": "memory"}))
        t.via = "said by you"
        saved = []
        for fact, topic in facts:
            result = await self._approve_and_run(tool, {"fact": fact, "topic": topic}, t)
            if (body := _unfence(result)) is not None and not body.startswith("ERROR"):
                saved.append(fact)
        if not saved:
            return None
        return self._reply(t, "Noted, sir: " + "; ".join(s.rstrip(".") for s in saved) + ".")

    async def _fast(self, plan: FastPlan, t: "_Turn") -> str | None:
        """Run a fast-path plan through the gate; None (fall back to the model) if a step can't run."""
        tools = [(self.tools.get(name), args) for name, args in plan.steps]
        if any(tool is None or tool.name in self.hidden for tool, _ in tools):
            return None
        t.bus.publish(Event("thinking", t.source, t.request_id, {"step": 0, "route": "fast", "intent": plan.intent}))
        results = []
        for tool, args in tools:
            result = await self._approve_and_run(tool, args, t)
            body = _unfence(result)
            if body is None or body.startswith("ERROR"):
                log.info("fast path %s fell back to the model: %.120s", plan.intent, body or result)
                return None
            results.append(result)
        try:
            reply = plan.say(results)
        except (ValueError, IndexError, KeyError) as e:
            log.warning("fast path %s couldn't word the reply: %s", plan.intent, e)
            return None
        log.info("fast path: %s", plan.intent)
        t.route, t.intent = "fast", plan.intent
        return self._reply(t, reply)

    def _needs_voice_yes(self, tool: Tool, args: dict, t: "_Turn") -> bool:
        """Satellite only: tools the policy would hold for a HUD approval, at one of the voice_confirm
        risks, are confirmed by a spoken yes instead (the rest still need the HUD or phone)."""
        return (t.source is Source.SATELLITE and bool(t.session) and self.sessions is not None and not t.dry_run
                and tool.risk in self.voice_confirm
                and self.policy.decide(tool, args, t.source, self.audit).action == "ask")

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

    async def _approve_and_run(self, tool: Tool, args: dict, t: "_Turn", hold: str = "") -> str:
        approved_by = await self._approve(tool, args, t, hold)
        if approved_by.startswith("ERROR"):
            return approved_by
        return await self._run(tool, args, approved_by, t)

    async def _approve(self, tool: Tool, args: dict, t: "_Turn", hold: str = "") -> str:
        """Who let this run ("policy", "web", "phone", "grant"...), or an ERROR when nobody did.
        hold: why a hook wants a human to decide (e.g. outside text in the arguments)."""
        name = tool.name  # audit, events and approvals use the internal name
        t.bus.publish(Event("tool_call", t.source, t.request_id,
                            {"tool": name, "origin": tool.origin, "risk": tool.risk, "arguments": args}))
        if t.dry_run and tool.risk is not Risk.READ:
            t.trace.append({"tool": name, "decision": "dry-run", "rule": "dry run", "why": ""})
            return "dry-run"  # recorded, never run: no approval needed
        d = self.policy.decide(tool, args, t.source, self.audit)
        if hold and d.action in ("allow", "dry-run"):
            d = PolicyDecision("ask", hold, "hook")
        t.trace.append({"tool": name, "decision": d.action, "rule": d.rule, "why": d.why})
        if d.action == "deny":
            self.audit.record(request_id=t.request_id, source=t.source, tool=name, risk=tool.risk, arguments=args,
                              status="blocked", approved_by=None, result=d.why or d.rule)
            t.bus.publish(Event("tool_result", t.source, t.request_id, {"tool": name, "status": "blocked"}))
            return (f"ERROR: that isn't allowed ({d.why or 'blocked by the policy'}). Tell the user briefly; "
                    "don't retry.")
        if d.action == "dry-run":
            return "dry-run"
        if d.action == "allow":
            return {"default": "policy", "self-scoped": "policy:self-scoped", "grant": "grant"}.get(
                d.rule.split(" ")[0], f"policy:{d.rule}")
        if t.source is Source.SATELLITE:  # a satellite can't approve: say where to, and read it back
            t.on_event({"say": f"Please approve on your phone or the HUD: {_describe(tool, args)}."})
        decision = await self.gate.request(request_id=t.request_id, source=t.source, tool=name, risk=tool.risk,
                                           arguments=args, reason=d.why, says=_describe(tool, args))
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
        if approved_by == "dry-run":
            output, status = f"(dry run: {tool.model_name} was recorded, not run.) Reply as if it was done.", "dry-run"
        else:
            try:
                output = await tool.call(args, ToolCtx(t.bus, t.source, t.request_id, via=t.via))
                status = "error" if output.startswith("ERROR") else "ok"
            except Exception as e:
                log.exception("tool %s failed", name)
                output, status = f"ERROR: {e}", "error"
        output = output[:MAX_TOOL_RESULT_CHARS]
        for hook in POST_TOOL:  # guard/hooks.py: records outside text (taint)
            output = hook(tool, args, output, t)
        t.seen.append(output)
        if status in ("ok", "dry-run") and (tool.risk is not Risk.READ or "screenshot" in name):  # screenshots save
            t.changed = True
        self.audit.record(request_id=t.request_id, source=t.source, tool=name, risk=tool.risk,
                          arguments=args, status=status, approved_by=approved_by,
                          result=audit_text(tool, output))
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
        # how this reply came about, for the HUD's "why" line: route, tools and who allowed them, time
        t.bus.publish(Event("trace", t.source, t.request_id, {
            "route": t.route, "intent": t.intent, "model": t.model if t.route == "model" else "",
            "seconds": round(time.time() - t.started, 1), "tools": t.trace, "dry_run": t.dry_run}))
        return text


@dataclass
class _Turn:
    source: Source
    request_id: str
    bus: EventBus
    on_text: Callable[[str | None], None] | None = None
    on_event: Callable[[dict], None] = lambda e: None
    session: str | None = None
    changed: bool = False  # a tool that changes something ran successfully this turn
    dry_run: bool = False
    heard: str = ""  # the user's words this conversation (slot checks)
    seen: list[str] = field(default_factory=list)  # tool outputs this turn
    tainted: list[str] = field(default_factory=list)  # outside text this turn (web, messages, files)
    trace: list[dict] = field(default_factory=list)  # policy decisions, for the HUD's "why" panel
    via: str = ""  # provenance for memories saved this turn
    route: str = "model"  # fast | memory | confirm | model
    intent: str = ""
    model: str = ""
    started: float = field(default_factory=time.time)


def _tokens(m: dict) -> int:
    """Rough token count (about 3.5 characters per token for English and JSON)."""
    return int(len(json.dumps(m, ensure_ascii=False)) / 3.5) + 4


def _fit(convo: list[dict], schemas: list[dict], budget: int) -> None:
    """Keep the conversation inside the model's context window, so the system prompt (and its
    data-not-instructions rules) is never what gets cut. Drops the oldest turns before the latest
    user message first, then shortens older tool results, then the newest ones."""
    if budget <= 0:
        return
    room = budget - REPLY_RESERVE - sum(_tokens(s) for s in schemas)
    used = lambda: sum(_tokens(m) for m in convo)  # noqa: E731
    last_user = max(i for i, m in enumerate(convo) if m["role"] == "user") if any(
        m["role"] == "user" for m in convo) else len(convo)
    while used() > room and last_user > 1:
        del convo[1]  # oldest history after the system prompt
        last_user -= 1
    tools = [m for m in convo if m["role"] == "tool"]
    for limit, group in ((600, tools[:-1]), (1500, tools[-1:]), (400, tools)):
        for m in group:
            if used() <= room:
                return
            if len(m["content"]) > limit:
                m["content"] = m["content"][:limit] + "\n…(cut to fit)\n</tool_result>"


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
    target = next((str(args[k]) for k in ("name", "page", "to", "query", "app", "command") if args.get(k)), "")
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
