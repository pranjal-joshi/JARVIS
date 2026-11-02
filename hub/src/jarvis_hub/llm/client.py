"""Minimal OpenAI-compatible client (chat + embeddings). Works with Ollama,
llama.cpp, vLLM or a cloud endpoint; nothing here is Ollama-specific."""

import json
import logging
import os
from collections.abc import Callable

import anthropic
import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 180,
                 extra_body: dict | None = None, per_model: dict | None = None,
                 endpoints: dict | None = None) -> None:
        self.model = model
        self._extra = extra_body or {}
        self._per_model = per_model or {}  # model -> extra fields overriding extra_body
        self._claude = None  # ClaudeChat, set by use_claude(); used while self.model is a Claude id
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=10),
        )
        # models served elsewhere (LLM_ENDPOINTS): picker name -> (client, model id on that server)
        self._remote: dict[str, tuple[httpx.AsyncClient, str]] = {}
        for name, ep in (endpoints or {}).items():
            key = os.environ.get(ep.get("key_env") or "", "") or "none"
            self._remote[name] = (httpx.AsyncClient(base_url=ep["base_url"].rstrip("/"),
                                                    headers={"Authorization": f"Bearer {key}"},
                                                    timeout=httpx.Timeout(timeout_s, connect=10)),
                                  ep.get("model") or name)

    def _route(self) -> tuple[httpx.AsyncClient, str]:
        """The server and model id for the selected model."""
        return self._remote.get(self.model, (self._http, self.model))

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   on_text: Callable[[str], None] | None = None) -> dict:
        """Return the assistant message dict (content and/or tool_calls).
        With on_text, the reply is streamed and on_text gets the content so far as it grows."""
        if self.model.startswith("claude-"):
            if self._claude is None:
                raise LLMError("a Claude model is selected but ANTHROPIC_API_KEY is not set")
            try:
                return await self._claude.chat(self.model, messages, tools, on_text)
            except anthropic.APIError as e:
                raise LLMError(f"Claude request failed: {type(e).__name__}: {getattr(e, 'message', e)}") from e
        http, model_id = self._route()
        body = {"model": model_id, "messages": messages, **self._extra, **self._per_model.get(self.model, {})}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if on_text is not None:
            try:
                return await self._stream(body, on_text)
            except LLMError as e:
                if not tools or "500" not in str(e):
                    raise
                log.warning("tool call unparseable; retrying without tools")
                return await self._stream({k: v for k, v in body.items() if k not in ("tools", "tool_choice")},
                                          on_text)
        try:
            r = await http.post("/chat/completions", json=body)
            if r.status_code == 500 and tools:  # Ollama can't parse the model's tool call: answer without tools
                log.warning("tool call unparseable (%s); retrying without tools", r.text[:200])
                r = await http.post("/chat/completions", json={k: v for k, v in body.items()
                                                                      if k not in ("tools", "tool_choice")})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"LLM request failed: {e}") from e
        msg = r.json()["choices"][0]["message"]
        # Some servers return arguments as dicts; normalise to JSON strings.
        for call in msg.get("tool_calls") or []:
            args = call.get("function", {}).get("arguments")
            if not isinstance(args, str):
                call["function"]["arguments"] = json.dumps(args or {})
        return msg

    async def json(self, messages: list[dict], schema: dict) -> dict:
        """A reply that is always valid JSON for schema (Ollama enforces it while decoding). Local models only."""
        if self.model.startswith("claude-"):
            raise LLMError("schema-constrained output is used with local models only")
        http, model_id = self._route()
        body = {"model": model_id, "messages": messages, **self._extra, **self._per_model.get(self.model, {}),
                "temperature": 0, "response_format": {"type": "json_schema",
                                                      "json_schema": {"name": "out", "schema": schema}}}
        try:
            r = await http.post("/chat/completions", json=body)
            r.raise_for_status()
            return json.loads(r.json()["choices"][0]["message"]["content"] or "{}")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise LLMError(f"JSON request failed: {e}") from e

    async def _stream(self, body: dict, on_text: Callable[[str], None]) -> dict:
        content, calls = "", {}  # tool calls arrive in pieces keyed by index
        try:
            async with self._route()[0].stream("POST", "/chat/completions", json={**body, "stream": True}) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:") or line.strip() == "data: [DONE]":
                        continue
                    choice = (json.loads(line[5:]).get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    for tc in delta.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", len(calls)), {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["function"]["name"] += fn.get("name") or ""
                        args = fn.get("arguments")
                        c["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args or {})
                    if piece := delta.get("content"):
                        content += piece
                        if not calls:
                            on_text(content)
        except httpx.HTTPError as e:
            raise LLMError(f"LLM request failed: {e}") from e
        msg = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        return msg

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        try:
            r = await self._http.post("/embeddings", json={"model": model, "input": texts})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"embedding request failed: {e}") from e
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    def use_claude(self, api_key: str, effort: str) -> None:
        from jarvis_hub.llm.claude import ClaudeChat
        self._claude = ClaudeChat(api_key, effort)

    async def loaded(self) -> bool | None:
        """Is the current local model in memory (warm)? None when the server can't say (not Ollama)."""
        if self.model.startswith("claude-") or self.model in self._remote:
            return None
        try:
            r = await self._http.get(self._base.removesuffix("/v1") + "/api/ps", timeout=3)
            names = [m.get("name", "") for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return None
        return any(n == self.model or n.split(":")[0] == self.model for n in names)

    async def ping(self) -> bool:
        if self.model.startswith("claude-"):
            return self._claude is not None
        try:
            r = await self._route()[0].get("/models", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()
        for http, _ in self._remote.values():
            await http.aclose()
