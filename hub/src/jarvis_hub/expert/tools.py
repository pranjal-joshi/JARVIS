"""ask_expert: consult a stronger model (Claude Code or opencode on the host) via the expert bridge.

Risk is `external`: the question leaves this machine, so every call waits for HUD approval,
and the approval card shows exactly what will be sent. The bridge runs the CLIs answer-only
(no tools), so the expert can't touch files or run commands."""

import httpx

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Settings
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

ENGINES = ["claude", "opencode"]
DESCRIPTION = (
    "Ask a much stronger cloud model for help. Use ONLY for questions you cannot answer well yourself: "
    "complex reasoning, maths, coding, technical explanations, or detailed general knowledge. "
    "Never use it for home control, the user's notes, small talk or simple facts. "
    "The user must approve each call and sees exactly what is sent, so put only what is needed in "
    "`question` and `context`; never include secrets or personal data the question doesn't need. "
    "engine: 'claude' (Claude Code, strongest) or 'opencode' (free models, slower).")


def register_expert_tool(reg: ToolRegistry, s: Settings) -> bool:
    token = s.expert_bridge_token.get_secret_value()
    if not token or token == "change-me":
        return False
    url = s.expert_bridge_url.rstrip("/") + "/v1/ask"
    timeout = httpx.Timeout(s.expert_timeout_s + 15, connect=5)

    async def ask_expert(args: dict, ctx: ToolCtx) -> str:
        question = str(args.get("question", "")).strip()
        if not question:
            return "ERROR: question is empty"
        engine = str(args.get("engine") or s.expert_default_engine)
        if engine not in ENGINES:
            engine = s.expert_default_engine
        body = {"question": question, "context": str(args.get("context") or ""), "engine": engine}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            return f"ERROR: the expert bridge is not reachable ({type(e).__name__}). Is it running on the host?"
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            return f"ERROR: expert unavailable: {data.get('detail') or r.status_code}"
        # returned to the user as-is (final_answer): the small local model paraphrases it poorly
        return (str(data.get("answer", "")).strip()
                + f"\n\n— {data.get('engine')} · {data.get('model')} ({data.get('seconds')} s)")

    reg.add(Tool(
        name="ask_expert",
        origin="expert",
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, self-contained."},
                "context": {"type": "string", "description": "Optional facts the expert needs."},
                "engine": {"type": "string", "enum": ENGINES},
            },
            "required": ["question"],
        },
        risk=Risk.EXTERNAL,
        call=ask_expert,
        final_answer=True,
    ))
    return True
