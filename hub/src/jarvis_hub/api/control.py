"""Hub endpoints for the UI and phone: approvals, audit, knowledge base, voice, status."""

import asyncio
import logging
import time
from datetime import date

import httpx

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
log = logging.getLogger(__name__)


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


@router.get("/kb/page")
async def kb_page(name: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    g = request.app.state.kb_graph
    page = g.page(name)
    if page is None:
        raise HTTPException(404, "no such page")
    return {"name": page.name, "journal": page.is_journal, "link": g.deep_link(page.name),
            "blocks": [b.text for b in page.blocks][:400],
            "links": sorted({l for b in page.blocks for l in b.links})}


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
    spoken = to_speech(body.input[:6000])
    if not spoken:  # nothing sayable (e.g. a code-only reply): stay quiet
        return Response(status_code=204)
    try:
        audio, engine = await request.app.state.voice.speak(spoken)
    except VoiceError as e:
        raise HTTPException(502, str(e)) from e
    return Response(audio, media_type="audio/wav", headers={"X-TTS-Engine": engine})


# ---------- devices (HUD Devices panel; pairing and allowing are never model tools) ----------
async def _device_bridge(request: Request, action: str, args: dict | None = None):
    s = request.app.state.settings
    token = s.device_bridge_token.get_secret_value()
    if not token:
        raise HTTPException(503, "device bridge not configured (DEVICE_BRIDGE_TOKEN)")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=5)) as c:
            r = await c.post(s.device_bridge_url.rstrip("/") + "/v1/act", json={"action": action, "args": args or {}},
                             headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"device bridge not running on this PC ({type(e).__name__}). Start it with: "
                                 "uv run --no-project python bridge/device_bridge.py") from e
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        raise HTTPException(r.status_code, body.get("detail") or "device bridge error")
    return body.get("result")


@router.get("/devices")
async def devices(request: Request, _: Source = Depends(require_approver)) -> list:
    return await _device_bridge(request, "adb_list")


@router.post("/devices/pair")
async def devices_pair(request: Request, _: Source = Depends(require_approver)) -> dict:
    return await _device_bridge(request, "adb_pair_start")


@router.get("/devices/pair/{job}")
async def devices_pair_status(job: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    return await _device_bridge(request, "adb_pair_status", {"id": job})


class AllowBody(BaseModel):
    serial: str
    allowed: bool = True


@router.post("/devices/allow")
async def devices_allow(body: AllowBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    return {"result": await _device_bridge(request, "adb_allow", body.model_dump())}


class DisconnectBody(BaseModel):
    id: str


@router.post("/devices/disconnect")
async def devices_disconnect(body: DisconnectBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    return {"result": await _device_bridge(request, "adb_disconnect", body.model_dump())}


# ---------- skills (KB pages under skills/, run on events once enabled here) ----------
@router.get("/skills")
async def skills_list(request: Request, _: Source = Depends(require_approver)) -> dict:
    eng = request.app.state.skills
    return {"skills": eng.skills(), "events": eng.recent}


class EnableBody(BaseModel):
    enabled: bool


@router.post("/skills/{name:path}/enable")
async def skills_enable(name: str, body: EnableBody, request: Request,
                        _: Source = Depends(require_approver)) -> dict:
    try:
        return request.app.state.skills.set_enabled(name, body.enabled)
    except KeyError:
        raise HTTPException(404, "no such skill page") from None


@router.post("/skills/{name:path}/run")
async def skills_run(name: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    eng = request.app.state.skills
    skill = next((s for s in eng.skills() if s["name"].lower() == name.lower()), None)
    if skill is None:
        raise HTTPException(404, "no such skill page")
    if not skill["enabled"]:
        raise HTTPException(409, "enable the skill first")
    eng._spawn(skill, "a manual run from the HUD")
    return {"ok": True}


# ---------- status ----------
@router.get("/status")
async def status(request: Request, _: Source = Depends(require_source)) -> dict:
    s = request.app.state
    return {
        "time": time.time(),
        "llm": {"model": s.llm.model, "up": await s.llm.ping()} if s.llm else None,
        "mcp": s.tools.status,
        "tools": s.tools.describe(s.settings.ask_for(), s.settings.hidden_tools()),
        "kb": {"graph": s.kb_graph.name, "vector": s.kb_index.vector_ready,
               "last_sync": s.kb_index.last_sync},
        "voice": await s.voice.status(),
        "pending_approvals": len(s.gate.pending()),
    }


# ---------- model choice ----------
class ModelBody(BaseModel):
    model: str


@router.get("/llm")
async def llm_get(request: Request, _: Source = Depends(require_source)) -> dict:
    s = request.app.state
    return {"model": s.llm.model, "models": s.settings.llm_choices()}


@router.post("/llm")
async def llm_set(body: ModelBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    s = request.app.state
    if body.model not in s.settings.llm_choices():
        raise HTTPException(400, "model not in LLM_MODELS")
    s.llm.model = body.model
    s.llm_choice_file.write_text(body.model)
    log.info("LLM switched to %s", body.model)

    async def warm() -> None:  # load it now, so the next reply doesn't pay the load time
        try:
            await s.llm.chat([{"role": "user", "content": "ok"}])
        except Exception as e:
            log.info("warm-up of %s failed: %s", body.model, e)

    asyncio.create_task(warm())
    return {"model": s.llm.model, "models": s.settings.llm_choices()}
