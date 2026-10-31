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
        for step in range(MAX_STEPS):
            try:
                msg = await self.llm.chat(convo, schemas)
            except LLMError as e:
                log.warning("%s", e)
                return self._reply(bus, source, request_id,
                                   "My apologies, sir, the language model is not answering.")
            calls = msg.get("tool_calls") or []
            if not calls:
                return self._reply(bus, source, request_id,
                                   _strip_think(msg.get("content") or "").strip() or "Done, sir.")
            convo.append({"role": "assistant", "content": msg.get("content") or "",
                          "tool_calls": calls})
            for call in calls:
                result = await self._run_tool(call, source, request_id, bus)
                convo.append({"role": "tool", "tool_call_id": call.get("id", ""),
                              "content": result})
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


def _strip_think(text: str) -> str:
    """Drop <think>...</think> blocks some local models emit inline."""
    while "<think>" in text and "</think>" in text:
        start, end = text.index("<think>"), text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text
