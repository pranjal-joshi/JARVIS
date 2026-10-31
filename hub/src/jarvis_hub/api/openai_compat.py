"""OpenAI-compatible surface so Home Assistant Assist (or any OpenAI client)
can use the hub as a conversation agent. Client-supplied tools are ignored:
the hub acts only through its own allow-listed tools and approval gate."""

import json
import time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from jarvis_hub.api.auth import require_source
from jarvis_hub.config import Source

router = APIRouter(prefix="/v1")
MODEL_ID = "jarvis"


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None


class ChatRequest(BaseModel):
    model: str = MODEL_ID
    messages: list[ChatMessage]
    stream: bool = False


def _text(content) -> str:
    if isinstance(content, list):  # OpenAI content parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


@router.get("/models")
async def models(_: Source = Depends(require_source)) -> dict:
    return {"object": "list", "data": [
        {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "jarvis"}]}


@router.post("/chat/completions")
async def chat(body: ChatRequest, request: Request,
               source: Source = Depends(require_source)):
    request_id = uuid.uuid4().hex
    messages = [{"role": m.role, "content": _text(m.content)} for m in body.messages]
    reply = await request.app.state.agent.run(
        messages=messages, source=source, request_id=request_id, bus=request.app.state.bus)
    created, cid = int(time.time()), f"chatcmpl-{request_id}"

    if body.stream:
        def chunk(delta: dict, finish: str | None = None) -> str:
            return "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": MODEL_ID,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

        async def gen():
            yield chunk({"role": "assistant"})
            yield chunk({"content": reply})
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return {
        "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": reply}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        # Non-standard; OpenAI clients ignore it. Lets the UI correlate events.
        "jarvis": {"request_id": request_id, "source": source},
    }
