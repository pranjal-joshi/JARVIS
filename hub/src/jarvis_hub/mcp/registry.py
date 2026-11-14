"""Tool registry. Holds built-in tools and tools from MCP servers listed in
mcp/servers.yaml. Only tools matched by a server's allow-list are exposed,
each tagged with a risk level; anything unlisted stays invisible to the model."""

import fnmatch
import logging
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx2
import yaml
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from jarvis_hub.approval.gate import Risk
from jarvis_hub.tools.cards import card
from jarvis_hub.tools.names import internal_name, model_name

log = logging.getLogger(__name__)

@dataclass
class ToolCtx:
    """Per-call context so tools can publish events for the right request."""

    bus: object
    source: str
    request_id: str
    via: str = ""  # where a saved fact came from ("said by you"...), for the memory's provenance
    session: str = ""  # the satellite conversation the call came from ("" = HUD, phone, a skill)
    said: str = ""  # the user's latest words (a page they named may be made; one the model made up may not)
    source_ref: str = ""  # the raw/ page a fact saved from earlier output came from (written as source::)


ToolFn = Callable[[dict, ToolCtx], Awaitable[str]]


@dataclass
class Tool:
    name: str          # name the model sees, e.g. homeassistant__HassTurnOn
    origin: str        # "builtin" or the MCP server key
    description: str
    parameters: dict
    risk: Risk
    call: ToolFn
    # Writes confined to the agent's own jarvis/ namespace and journal: audited, not gated.
    self_scoped: bool = False
    # The tool's own output is the reply (e.g. an expert answer the small model would only paraphrase badly).
    final_answer: bool = False
    # Always asks (an approval card), whatever the policy's default says; why, for the card. A rule that denies
    # still denies, and an "allow for 1 hour" grant from a card still lets it through.
    confirm: str = ""

    @property
    def model_name(self) -> str:
        return model_name(self.name)

    def openai_schema(self) -> dict:
        c = card(self.name)  # a tool's card may say what it is NOT for
        desc = self.description + (f" Not for: {c.not_for}." if c and c.not_for else "")
        return {"type": "function", "function": {
            "name": self.model_name, "description": desc[:1024],
            "parameters": self.parameters or {"type": "object", "properties": {}}}}


_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def classify(tool_name: str, allow: dict[str, str], hinted: Risk | None) -> Risk | None:
    """First matching allow-list pattern wins. MCP annotations may raise the risk, never lower it."""
    for pattern, risk in allow.items():
        if fnmatch.fnmatchcase(tool_name, pattern):
            base = Risk(risk)
            if hinted and hinted.rank > base.rank:
                return hinted
            return base
    return None


def _hinted_risk(tool) -> Risk | None:
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return None
    if getattr(ann, "destructive_hint", None):
        return Risk.DESTRUCTIVE
    if getattr(ann, "read_only_hint", None) is False:
        return Risk.WRITE
    return None


def _text(result) -> str:
    parts = [getattr(b, "text", None) or f"[{getattr(b, 'type', 'content')}]"
             for b in (result.content or [])]
    out = "\n".join(parts) or str(getattr(result, "structured_content", "") or "")
    return ("ERROR: " + out) if getattr(result, "is_error", False) else out


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._stack = AsyncExitStack()
        self.status: dict[str, str] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """By internal name or by the name the model sees."""
        return self._tools.get(name) or self._tools.get(internal_name(name))

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def describe(self, ask_for: frozenset = frozenset(), hidden: frozenset = frozenset()) -> list[dict]:
        return [{"name": t.name, "origin": t.origin, "risk": t.risk,
                 "asks": bool(t.confirm) or t.risk in ask_for and not t.self_scoped, "hidden": t.name in hidden,
                 "description": t.description.split(". ")[0][:160]} for t in self.all()]

    async def load_mcp(self, config_path: str) -> None:
        if not os.path.exists(config_path):
            log.warning("no MCP config at %s", config_path)
            return
        with open(config_path, encoding="utf-8") as f:
            cfg = _expand(yaml.safe_load(f) or {})
        for key, server in (cfg.get("servers") or {}).items():
            if not server.get("enabled", True):
                self.status[key] = "disabled"
                continue
            missing = [e for e in server.get("requires_env", []) if not os.environ.get(e)]
            if missing:
                self.status[key] = f"skipped: missing {', '.join(missing)}"
                continue
            try:
                await self._connect(key, server)
            except Exception as e:  # one bad server must not take the hub down
                log.exception("MCP server %s failed", key)
                self.status[key] = f"error: {e}"

    async def _connect(self, key: str, server: dict) -> None:
        if server["transport"] == "http":
            headers = {}
            if token := server.get("bearer_token"):
                headers["Authorization"] = f"Bearer {token}"
            http = await self._stack.enter_async_context(httpx2.AsyncClient(
                headers=headers, timeout=httpx2.Timeout(30.0, read=300.0)))
            target = streamable_http_client(server["url"], http_client=http)
        elif server["transport"] == "stdio":
            target = StdioServerParameters(command=server["command"],
                                           args=server.get("args", []),
                                           env=server.get("env") or None)
        else:
            raise ValueError(f"unknown transport {server['transport']}")

        client = await self._stack.enter_async_context(Client(target))
        tools, cursor = [], None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        allow = server.get("allow") or {}
        exposed = 0
        for t in tools:
            risk = classify(t.name, allow, _hinted_risk(t))
            if risk is None:
                continue

            async def call(args: dict, _ctx: ToolCtx, _name=t.name) -> str:
                return _text(await client.call_tool(_name, args))

            self.add(Tool(f"{key}__{t.name}", key, t.description or t.title or t.name,
                          t.input_schema or {}, risk, call))
            exposed += 1
        self.status[key] = f"connected: {exposed}/{len(tools)} tools allowed"
        log.info("MCP %s: %s", key, self.status[key])

    async def aclose(self) -> None:
        await self._stack.aclose()
