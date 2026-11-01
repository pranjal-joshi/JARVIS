"""Claude (Anthropic Messages API, official SDK) behind the same chat() shape as the local client.

The agent speaks OpenAI-style messages (role/content/tool_calls, role "tool" results). This adapter
converts them to Messages API blocks and back, so the agent loop, approvals and audit are unchanged.
An assistant turn keeps Claude's own content blocks (`_claude`, incl. thinking) so the tool loop
replays them exactly, as the API requires.

Privacy: with a Claude model selected, the conversation and every tool result in it (notes,
notifications, messages) go to Anthropic. The local model never sends anything off the machine.
"""

import json
import logging
from collections.abc import Callable

import anthropic

log = logging.getLogger(__name__)
MAX_TOKENS = 4096  # spoken replies are short; a tool turn needs only a little more


def _tools(openai_tools: list[dict] | None) -> list[dict]:
    out = []
    for t in openai_tools or []:
        fn = t.get("function", {})
        out.append({"name": fn["name"], "description": fn.get("description", "")[:1024],
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _messages(openai_msgs: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-style history -> (system text, Messages API turns). Consecutive same-role turns merge."""
    system, turns = [], []

    def add(role: str, blocks: list[dict]) -> None:
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": list(blocks)})

    for m in openai_msgs:
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            system.append(content)
        elif role == "user":
            add("user", [{"type": "text", "text": content}] if content else [])
        elif role == "assistant":
            if m.get("_claude"):  # Claude's own blocks from an earlier step: replay unchanged
                add("assistant", m["_claude"])
                continue
            blocks = [{"type": "text", "text": content}] if content.strip() else []
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": call.get("id") or fn.get("name", "call"),
                               "name": fn.get("name", ""), "input": args})
            if blocks:
                add("assistant", blocks)
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": content}])
    return "\n\n".join(system), [t for t in turns if t["content"]]


class ClaudeChat:
    def __init__(self, api_key: str, effort: str = "low", timeout_s: float = 120) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=api_key or None, timeout=timeout_s, max_retries=2)
        self.effort = effort

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                   on_text: Callable[[str], None] | None = None) -> dict:
        system, turns = _messages(messages)
        params = dict(model=model, max_tokens=MAX_TOKENS, system=system, messages=turns,
                      thinking={"type": "adaptive"}, output_config={"effort": self.effort})
        if tools:
            params["tools"] = _tools(tools)
        text = ""
        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "text":
                    text += event.text
                    if on_text:
                        on_text(text)
            final = await stream.get_final_message()

        if final.stop_reason == "refusal":
            log.info("claude declined (%s)", getattr(final.stop_details, "category", None))
            return {"role": "assistant", "content": "I'm afraid I can't help with that one, sir."}
        if final.stop_reason == "max_tokens" and any(b.type == "tool_use" for b in final.content):
            return {"role": "assistant", "content": "That reply ran too long to finish, sir. Shall I try it shorter?"}
        calls = [{"id": b.id, "type": "function",
                  "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                 for b in final.content if b.type == "tool_use"]
        msg = {"role": "assistant", "content": "".join(b.text for b in final.content if b.type == "text"),
               "_claude": [b.model_dump(exclude_none=True) for b in final.content]}
        if calls:
            msg["tool_calls"] = calls
        return msg
