"""OpenAI-compatible surface so Home Assistant Assist (or any OpenAI client)
can use the hub as a conversation agent. Client-supplied tools are ignored:
the hub acts only through its own allow-listed tools and approval gate."""

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from jarvis_hub.api.auth import require_source
from jarvis_hub.config import Source
from jarvis_hub.voice.speakable import for_ear

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
    session = None
    if source is Source.SATELLITE:  # one conversation per satellite, kept by the hub (see voice/session.py)
        session = "satellite:" + (request.headers.get("x-satellite-id") or "default")[:64]
        messages = request.app.state.sessions.merge(session, messages)
    heard = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    run = dict(messages=messages, source=source, request_id=request_id, bus=request.app.state.bus,
               session=session)

    def remember(reply: str) -> None:
        if session:
            request.app.state.sessions.record(session, heard, reply)
    created, cid = int(time.time()), f"chatcmpl-{request_id}"

    if body.stream:
        # Real streaming: reply text goes out as the model writes it, so a voice client can start
        # speaking the first sentence while the rest is generated.
        def chunk(delta: dict, finish: str | None = None, **extra) -> str:
            return "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": MODEL_ID, **extra,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

        queue: asyncio.Queue[str | dict | None] = asyncio.Queue()
        task = asyncio.create_task(request.app.state.agent.run(**run, on_text=queue.put_nowait,
                                                                on_event=queue.put_nowait))

        async def gen():
            yield chunk({"role": "assistant"})
            streamed = ""
            while not (task.done() and queue.empty()):
                try:
                    piece = await asyncio.wait_for(queue.get(), 0.25)
                except TimeoutError:
                    continue
                if isinstance(piece, dict):  # voice cue: a tool started, or a line to say now
                    yield chunk({}, jarvis=piece)
                    continue
                if piece is None:  # the agent took that back (a tool runs next): client drops it
                    streamed = ""
                    yield chunk({}, jarvis={"reset": True})
                    continue
                streamed += piece
                yield chunk({"content": piece})
            reply = task.result()
            remember(reply)
            if not streamed.strip():  # a tool's own answer or an error message: send it whole
                yield chunk({"content": reply})
            # Non-standard: the final text (it can differ from what streamed if a tool ran after the
            # model had started talking). OpenAI clients ignore unknown fields.
            yield chunk({}, "stop", jarvis={"request_id": request_id, "final": reply})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    reply = await request.app.state.agent.run(**run)
    remember(reply)
    if source is Source.SATELLITE:  # Home Assistant speaks this text as it is
        reply = for_ear(reply)
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": reply}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        # Non-standard; OpenAI clients ignore it. Lets the UI correlate events.
        "jarvis": {"request_id": request_id, "source": source},
    }
