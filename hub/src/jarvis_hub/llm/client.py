"""Minimal OpenAI-compatible client (chat + embeddings). Works with Ollama,
llama.cpp, vLLM or a cloud endpoint; nothing here is Ollama-specific."""

import json
import logging

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 180,
                 extra_body: dict | None = None) -> None:
        self.model = model
        self._extra = extra_body or {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=10),
        )

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Return the assistant message dict (content and/or tool_calls)."""
        body = {"model": self.model, "messages": messages, **self._extra}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            r = await self._http.post("/chat/completions", json=body)
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

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        try:
            r = await self._http.post("/embeddings", json={"model": model, "input": texts})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"embedding request failed: {e}") from e
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def ping(self) -> bool:
        try:
            r = await self._http.get("/models", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()
