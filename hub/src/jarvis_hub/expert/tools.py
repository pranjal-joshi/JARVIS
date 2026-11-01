"""ask_claude / ask_opencode: consult a stronger model (Claude Code or opencode on the host) via the expert bridge.

Risk is `external`: the question leaves this machine, so every call waits for HUD approval,
and the approval card shows exactly what will be sent. The bridge runs the CLIs answer-only
(no tools), so the expert can't touch files or run commands."""

import httpx

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Settings
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

ENGINES = {
    "claude": ("ask_claude", "Ask Claude, a much stronger cloud model."),
    "opencode": ("ask_opencode", "Ask an opencode model (free tier, slower and quota-limited); "
                 "use only when the user asks for opencode or ask_claude is unavailable."),
}
COMMON = (
    " Use for: hard reasoning, coding, technical detail, long explanations you can't give well yourself. "
    "Not for notes, devices, small talk or simple facts. The user approves each call and sees what is "
    "sent: include only what the question needs, never secrets or personal data.")


def register_expert_tool(reg: ToolRegistry, s: Settings) -> bool:
    """Adds ask_claude and ask_opencode (one tool per engine, so the model picks by name)."""
    token = s.expert_bridge_token.get_secret_value()
    if not token or token == "change-me":
        return False
    url = s.expert_bridge_url.rstrip("/") + "/v1/ask"
    timeout = httpx.Timeout(s.expert_timeout_s + 15, connect=5)

    def make(engine: str):
        async def ask(args: dict, ctx: ToolCtx) -> str:
            question = str(args.get("question", "")).strip()
            if not question:
                return "ERROR: question is empty"
            body = {"question": question, "context": str(args.get("context") or ""), "engine": engine}
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                return f"ERROR: the expert bridge is not reachable ({type(e).__name__}). Is it running on the host?"
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200:
                return f"ERROR: {engine} unavailable: {data.get('detail') or r.status_code}"
            # returned to the user as-is (final_answer): the small local model paraphrases it poorly
            return (str(data.get("answer", "")).strip()
                    + f"\n\n— {data.get('engine')} · {data.get('model')} ({data.get('seconds')} s)")
        return ask

    for engine, (name, what) in ENGINES.items():
        reg.add(Tool(
            name=name,
            origin="expert",
            description=what + COMMON,
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question, self-contained."},
                    "context": {"type": "string", "description": "Optional facts the expert needs."},
                },
                "required": ["question"],
            },
            risk=Risk.EXTERNAL,
            call=make(engine),
            final_answer=True,
        ))
    return True
