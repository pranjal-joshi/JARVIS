"""Minimal OpenAI-compatible client (chat + embeddings). Works with Ollama,
llama.cpp, vLLM or a cloud endpoint; nothing here is Ollama-specific."""

import json
import logging
import os
import re
from collections.abc import Callable

import anthropic
import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class RateLimited(LLMError):
    """The model's server said too many requests (HTTP 429). retry_s: when it says to try again."""

    def __init__(self, model: str, retry_s: float) -> None:
        super().__init__(f"{model} is rate limited (try again in {retry_s:.0f} s)")
        self.model, self.retry_s = model, retry_s


def _retry_s(headers) -> float:
    """Seconds until the server takes requests again: Retry-After, else the token or request reset
    ("2.137s", "2m52.8s", "120ms" in Groq's and OpenAI's x-ratelimit-reset-* headers)."""
    try:
        return float(headers.get("retry-after", ""))
    except ValueError:
        pass
    waits = []
    for h in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        if m := re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?", headers.get(h, "")):
            h_, m_, s_, ms = (float(x) if x else 0.0 for x in m.groups())
            waits.append(h_ * 3600 + m_ * 60 + s_ + ms / 1000)
    return min((w for w in waits if w > 0), default=60.0)


# Sampling the model's makers recommend for tool use, sent unless LLM_EXTRA_BODY / LLM_MODEL_EXTRA say otherwise.
# LFM3 (LiquidAI model card, "chat, tool use, agents"): without these Ollama's own, much more random, defaults
# applied. top_k and repeat_penalty are Ollama's names; servers that don't know them ignore them.
SAMPLING = (("lfm3", {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1}),)


def sampling(model: str) -> dict:
    m = model.lower()
    return next((dict(s) for key, s in SAMPLING if key in m), {})


def _env_key(name: str) -> str:
    """An endpoint's API key (LLM_ENDPOINTS key_env): the environment, else .env. Docker puts .env into the
    environment; a hub started on the host only has Settings read .env, which skips names it has no field for."""
    if not name:
        return ""
    if v := os.environ.get(name, ""):
        return v
    from dotenv import dotenv_values
    return dotenv_values(".env").get(name) or ""


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
            key = _env_key(ep.get("key_env") or "") or "none"
            self._remote[name] = (httpx.AsyncClient(base_url=ep["base_url"].rstrip("/"),
                                                    headers={"Authorization": f"Bearer {key}"},
                                                    timeout=httpx.Timeout(timeout_s, connect=10)),
                                  ep.get("model") or name)

    @property
    def remote(self) -> bool:
        """The selected model is on another server (LLM_ENDPOINTS), often a rate-limited cloud one."""
        return self.model in self._remote

    def _route(self, model: str | None = None) -> tuple[httpx.AsyncClient, str]:
        """The server and model id for the selected model (or `model`, a picker name)."""
        model = model or self.model
        return self._remote.get(model, (self._http, model))

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   on_text: Callable[[str], None] | None = None, model: str | None = None) -> dict:
        """Return the assistant message dict (content and/or tool_calls).
        With on_text, the reply is streamed and on_text gets the content so far as it grows.
        model: a picker name to use for this call only (a turn sent to a bigger model); None = the selected one."""
        name = model or self.model
        if name.startswith("claude-"):
            if self._claude is None:
                raise LLMError("a Claude model is selected but ANTHROPIC_API_KEY is not set")
            try:
                return await self._claude.chat(name, messages, tools, on_text)
            except anthropic.APIError as e:
                raise LLMError(f"Claude request failed: {type(e).__name__}: {getattr(e, 'message', e)}") from e
        http, model_id = self._route(name)
        remote = name in self._remote
        body = {"model": model_id, "messages": messages, **sampling(model_id), **self._extra,
                **self._per_model.get(name, {})}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if on_text is not None:
            try:
                return await self._stream(body, on_text, name)
            except LLMError as e:
                # a cloud server (Groq) answers 400 when the model's tool call doesn't parse: it's sampled, so ask
                # once more; Ollama answers 500. Then answer without tools.
                cloud = "400" in str(e) and remote
                if not tools or not ("500" in str(e) or cloud):
                    raise
                if cloud:
                    log.warning("the model's tool call didn't parse (%s); asking again", e)
                    try:
                        return await self._stream(body, on_text, name)
                    except LLMError as again:
                        if "400" not in str(again):
                            raise
                log.warning("tool call unparseable; retrying without tools")
                return await self._stream({k: v for k, v in body.items() if k not in ("tools", "tool_choice")},
                                          on_text, name)
        try:
            r = await http.post("/chat/completions", json=body)
            if r.status_code == 429:
                raise RateLimited(name, _retry_s(r.headers))
            if r.status_code == 400 and tools and remote and "tool_use_failed" in r.text:
                log.warning("the model's tool call didn't parse (%s); asking again", r.text[:200])
                r = await http.post("/chat/completions", json=body)  # sampled again; a 400 again falls through
                if r.status_code == 400 and "tool_use_failed" in r.text:
                    r = await http.post("/chat/completions", json={k: v for k, v in body.items()
                                                                          if k not in ("tools", "tool_choice")})
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
            if r.status_code == 429:
                raise RateLimited(self.model, _retry_s(r.headers))
            r.raise_for_status()
            return json.loads(r.json()["choices"][0]["message"]["content"] or "{}")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise LLMError(f"JSON request failed: {e}") from e

    async def _stream(self, body: dict, on_text: Callable[[str], None], model: str | None = None) -> dict:
        content, calls = "", {}  # tool calls arrive in pieces keyed by index
        try:
            async with self._route(model)[0].stream("POST", "/chat/completions", json={**body, "stream": True}) as r:
                if r.status_code == 429:
                    raise RateLimited(model or self.model, _retry_s(r.headers))
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
