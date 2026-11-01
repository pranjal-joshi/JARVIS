"""Default agent loop: OpenAI-style function calling over the tool registry,
with every call passing the approval gate and landing in the audit log."""

import json
import logging
from datetime import datetime

from jarvis_hub.approval.gate import ApprovalGate, Risk
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.llm.client import LLMClient, LLMError
from jarvis_hub.mcp.registry import ToolCtx, ToolRegistry
from jarvis_hub.persona.jarvis import SYSTEM_PROMPT, source_hint

log = logging.getLogger(__name__)
MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 6000
FAKE_TOOL_MARKERS = ("<tool_result", "</tool_result>", "<tool_call", "<function_call", "<function=")
FAKE_TOOL_NUDGE = ("(system) Your last message wrote a tool call or tool result as plain text; no tool ran. "
                   "Call the tool through the tools interface, or answer without claiming any action happened.")


class ToolAgent:
    def __init__(self, llm: LLMClient, tools: ToolRegistry, gate: ApprovalGate,
                 audit: AuditStore) -> None:
        self.llm, self.tools, self.gate, self.audit = llm, tools, gate, audit

    async def run(self, *, messages: list[dict], source: Source, request_id: str,
                  bus: EventBus) -> str:
        # Client-supplied system prompts are dropped: the persona and rules are ours.
        convo = [{"role": "system", "content": SYSTEM_PROMPT + "\n" + source_hint(source)
                  + f"\nCurrent local time: {datetime.now():%A %d %B %Y, %H:%M}."}]
        convo += [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
        schemas = [t.openai_schema() for t in self.tools.all()]

        bus.publish(Event("thinking", source, request_id, {"step": 0}))
        faked = False
        for step in range(MAX_STEPS):
            try:
                msg = await self.llm.chat(convo, schemas)
            except LLMError as e:
                log.warning("%s", e)
                return self._reply(bus, source, request_id,
                                   "My apologies, sir, the language model is not answering.")
            calls = msg.get("tool_calls") or []
            if not calls:
                text = _strip_think(msg.get("content") or "").strip()
                if _fakes_tool_use(text):
                    # Small models sometimes write a tool call or result as text instead of calling
                    # the tool. Nothing ran, so never pass that on as if it had: ask once more.
                    log.warning("model wrote tool markup as text: %.200s", text)
                    if faked:
                        return self._reply(bus, source, request_id,
                                           "My apologies, sir, I couldn't carry that out. Nothing was changed.")
                    faked = True
                    convo.append({"role": "assistant", "content": text})
                    convo.append({"role": "user", "content": FAKE_TOOL_NUDGE})
                    continue
                return self._reply(bus, source, request_id, text or "Done, sir.")
            convo.append({"role": "assistant", "content": msg.get("content") or "",
                          "tool_calls": calls})
            final = None
            for call in calls:
                result = await self._run_tool(call, source, request_id, bus)
                convo.append({"role": "tool", "tool_call_id": call.get("id", ""),
                              "content": result})
                tool = self.tools.get(call.get("function", {}).get("name", ""))
                body = _unfence(result)
                if tool and tool.final_answer and body is not None and not body.startswith("ERROR"):
                    final = body
            if final is not None:
                return self._reply(bus, source, request_id, final)
            bus.publish(Event("thinking", source, request_id, {"step": step + 1}))
        return self._reply(bus, source, request_id,
                           "I seem to be going in circles, sir. Shall we try that differently?")

    async def _run_tool(self, call: dict, source: Source, request_id: str,
                        bus: EventBus) -> str:
        fn = call.get("function", {})
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            return "ERROR: arguments were not valid JSON"
        tool = self.tools.get(name)
        if tool is None:
            return f"ERROR: no such tool {name!r}"

        bus.publish(Event("tool_call", source, request_id,
                          {"tool": name, "origin": tool.origin, "risk": tool.risk, "arguments": args}))
        approved_by = "policy"
        if tool.self_scoped:
            approved_by = "policy:self-scoped"
        elif tool.risk is not Risk.READ:
            decision = await self.gate.request(request_id=request_id, source=source,
                                               tool=name, risk=tool.risk, arguments=args)
            approved_by = decision.by
            if not decision.approved:
                status = "timeout" if decision.by == "timeout" else "denied"
                self.audit.record(request_id=request_id, source=source, tool=name,
                                  risk=tool.risk, arguments=args, status=status,
                                  approved_by=None)
                bus.publish(Event("tool_result", source, request_id,
                                  {"tool": name, "status": status}))
                return f"ERROR: the user did not approve this action ({status}). Do not retry it."

        try:
            output = await tool.call(args, ToolCtx(bus, source, request_id))
            status = "error" if output.startswith("ERROR") else "ok"
        except Exception as e:
            log.exception("tool %s failed", name)
            output, status = f"ERROR: {e}", "error"
        output = output[:MAX_TOOL_RESULT_CHARS]
        self.audit.record(request_id=request_id, source=source, tool=name, risk=tool.risk,
                          arguments=args, status=status, approved_by=approved_by,
                          result=output[:2000])
        bus.publish(Event("tool_result", source, request_id,
                          {"tool": name, "status": status, "preview": output[:300]}))
        # Fence results so the model treats them as data, not instructions.
        return f"<tool_result tool=\"{name}\">\n{output}\n</tool_result>"

    @staticmethod
    def _reply(bus: EventBus, source: Source, request_id: str, text: str) -> str:
        bus.publish(Event("reply", source, request_id, {"text": text}))
        return text


def _unfence(result: str) -> str | None:
    """Tool output without the <tool_result> fence; None if the tool never ran."""
    if not result.startswith("<tool_result"):
        return None
    return result.split("\n", 1)[1].rsplit("\n</tool_result>", 1)[0].strip()


def _fakes_tool_use(text: str) -> bool:
    """True when a reply imitates tool markup instead of using the tools interface."""
    lowered = text.lower()
    return any(m in lowered for m in FAKE_TOOL_MARKERS)


def _strip_think(text: str) -> str:
    """Drop <think>...</think> blocks some local models emit inline."""
    while "<think>" in text and "</think>" in text:
        start, end = text.index("<think>"), text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text
