"""Hub endpoints for the UI and phone: approvals, audit, knowledge base, voice, status."""

import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from jarvis_hub.api.auth import require_source
from jarvis_hub.approval.gate import APPROVER_SOURCES
from jarvis_hub.config import Source
from jarvis_hub.kb.tools import recent_memory, write_daily_summary
from jarvis_hub.voice.service import VoiceError
from jarvis_hub.voice.speakable import to_speech

router = APIRouter(prefix="/v1")


def require_approver(source: Source = Depends(require_source)) -> Source:
    if source not in APPROVER_SOURCES:
        raise HTTPException(403, f"{source} may not use this endpoint")
    return source


# ---------- approvals ----------
class DecisionBody(BaseModel):
    approved: bool


@router.get("/approvals")
async def list_approvals(request: Request, _: Source = Depends(require_approver)) -> list:
    return request.app.state.gate.pending()


@router.post("/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionBody, request: Request,
                 source: Source = Depends(require_approver)) -> dict:
    if not request.app.state.gate.decide(approval_id, body.approved, source):
        raise HTTPException(404, "no such pending approval")
    return {"ok": True}


# ---------- audit ----------
@router.get("/audit")
async def audit(request: Request, limit: int = 50,
                _: Source = Depends(require_approver)) -> list:
    return request.app.state.audit.recent(min(limit, 500))


# ---------- knowledge base ----------
@router.get("/kb/search")
async def kb_search(q: str, request: Request, limit: int = 8,
                    _: Source = Depends(require_approver)) -> list:
    return await request.app.state.kb_index.search(q, min(limit, 50))


@router.get("/kb/graph")
async def kb_graph(request: Request, _: Source = Depends(require_approver)) -> dict:
    return request.app.state.kb_index.link_graph()


@router.get("/kb/memory")
async def kb_memory(request: Request, limit: int = 20,
                    _: Source = Depends(require_approver)) -> list:
    return recent_memory(request.app.state.kb_graph, min(limit, 100))


@router.get("/kb/tasks")
async def kb_tasks(request: Request, _: Source = Depends(require_source)) -> list:
    return [t.__dict__ for t in request.app.state.kb_graph.tasks()]


@router.post("/kb/sync")
async def kb_sync(request: Request, _: Source = Depends(require_approver)) -> dict:
    return await request.app.state.kb_index.sync()


@router.post("/kb/daily-summary")
async def daily_summary(request: Request, day: date | None = None,
                        _: Source = Depends(require_approver)) -> dict:
    page = write_daily_summary(request.app.state.kb_graph, request.app.state.audit, day)
    await request.app.state.kb_index.sync()
    return {"page": page}


# ---------- voice (web/phone path) ----------
MAX_AUDIO_BYTES = 10 * 1024 * 1024


@router.post("/audio/transcriptions")
async def transcribe(file: UploadFile, request: Request,
                     _: Source = Depends(require_source)) -> dict:
    data = await file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large")
    try:
        return {"text": await request.app.state.voice.transcribe(data)}
    except VoiceError as e:
        raise HTTPException(502, str(e)) from e


class SpeechBody(BaseModel):
    input: str
    model: str | None = None
    voice: str | None = None


@router.post("/audio/speech")
async def speech(body: SpeechBody, request: Request,
                 _: Source = Depends(require_source)) -> Response:
    if not body.input.strip():
        raise HTTPException(400, "empty input")
    try:
        audio, engine = await request.app.state.voice.speak(to_speech(body.input[:6000]))
    except VoiceError as e:
        raise HTTPException(502, str(e)) from e
    return Response(audio, media_type="audio/wav", headers={"X-TTS-Engine": engine})


# ---------- status ----------
@router.get("/status")
async def status(request: Request, _: Source = Depends(require_source)) -> dict:
    s = request.app.state
    return {
        "time": time.time(),
        "llm": {"model": s.settings.llm_model, "up": await s.llm.ping()} if s.llm else None,
        "mcp": s.tools.status,
        "tools": s.tools.describe(),
        "kb": {"graph": s.kb_graph.name, "vector": s.kb_index.vector_ready,
               "last_sync": s.kb_index.last_sync},
        "voice": await s.voice.status(),
        "pending_approvals": len(s.gate.pending()),
    }
