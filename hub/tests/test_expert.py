import asyncio

import httpx

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Settings
from jarvis_hub.expert import tools as expert
from jarvis_hub.mcp.registry import ToolRegistry


def test_off_without_token():
    reg = ToolRegistry()
    assert not expert.register_expert_tool(reg, Settings(expert_bridge_token=""))
    assert reg.get("ask_expert") is None


def test_external_risk_and_call(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"engine": "claude", "model": "m", "answer": "42", "seconds": 1})

    real = httpx.AsyncClient
    monkeypatch.setattr(expert.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    reg = ToolRegistry()
    assert expert.register_expert_tool(reg, Settings(expert_bridge_token="t0k"))
    tool = reg.get("ask_expert")
    assert tool.risk is Risk.EXTERNAL  # never auto-approved
    out = asyncio.run(tool.call({"question": "q", "engine": "nope"}, None))
    assert out.startswith("42") and "(1 s)" in out and seen["auth"] == "Bearer t0k"
    assert '"engine": "claude"' in seen["body"] or '"engine":"claude"' in seen["body"]  # unknown engine -> default


def test_bridge_down_is_reported(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    real = httpx.AsyncClient
    monkeypatch.setattr(expert.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    reg = ToolRegistry()
    expert.register_expert_tool(reg, Settings(expert_bridge_token="t0k"))
    out = asyncio.run(reg.get("ask_expert").call({"question": "q"}, None))
    assert out.startswith("ERROR: the expert bridge is not reachable")
