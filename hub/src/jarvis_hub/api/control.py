"""Hub endpoints for the UI and phone: approvals, audit, knowledge base, voice, status."""

import asyncio
import json
import logging
import time
import uuid
from datetime import date
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from jarvis_hub.api.auth import require_source
from jarvis_hub.approval.gate import APPROVER_SOURCES
from jarvis_hub.config import Source
from jarvis_hub.kb.tools import _today_title, recent_memory, write_daily_summary
from jarvis_hub.voice.service import VoiceError
from jarvis_hub.voice.speakable import for_ear, to_speech

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)


def require_approver(source: Source = Depends(require_source)) -> Source:
    if source not in APPROVER_SOURCES:
        raise HTTPException(403, f"{source} may not use this endpoint")
    return source


# ---------- approvals ----------
class DecisionBody(BaseModel):
    approved: bool
    allow_minutes: int | None = None  # "allow this tool for a while" (approved only, at most a day)


@router.get("/approvals")
async def list_approvals(request: Request, _: Source = Depends(require_approver)) -> list:
    return request.app.state.gate.pending()


@router.post("/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionBody, request: Request,
                 source: Source = Depends(require_approver)) -> dict:
    st = request.app.state
    p = st.gate.get(approval_id)
    if body.approved and body.allow_minutes and p is not None:
        st.policy.grant(p.tool, p.source.value, min(body.allow_minutes, 24 * 60))
    if not st.gate.decide(approval_id, body.approved, source):
        raise HTTPException(404, "no such pending approval")
    return {"ok": True}


# ---------- policy (read-only here: the file is edited by hand) ----------
@router.get("/policy")
async def policy(request: Request, _: Source = Depends(require_approver)) -> dict:
    return request.app.state.policy.describe()


@router.delete("/policy/grants/{tool}")
async def revoke_grant(tool: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    request.app.state.policy.revoke(tool)
    return {"ok": True}


# ---------- evals (scripts/eval posts its summary; the HUD shows them) ----------
@router.post("/evals")
async def eval_post(request: Request, _: Source = Depends(require_approver)) -> dict:
    body = await request.json()
    path = Path(request.app.state.settings.hub_db_path).with_name("evals.json")
    runs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    runs = [*runs, {k: body.get(k) for k in ("model", "when", "cases", "passed", "first_words_median_s",
                                             "by_category")} | {"failed": [
        {"id": r["id"], "fails": r["fails"]} for r in body.get("rows", []) if not r.get("pass")][:60]}][-30:]
    path.write_text(json.dumps(runs), encoding="utf-8")
    return {"ok": True}


@router.get("/evals")
async def eval_list(request: Request, _: Source = Depends(require_approver)) -> list:
    path = Path(request.app.state.settings.hub_db_path).with_name("evals.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


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


class MemoryEdit(BaseModel):
    page: str
    text: str  # the block as it is now (its first line is enough)
    new: str | None = None  # None = forget it


@router.post("/kb/memory/edit")
async def kb_memory_edit(body: MemoryEdit, request: Request, _: Source = Depends(require_approver)) -> dict:
    """The HUD's memory review: correct or forget one thing JARVIS learned (jarvis/ pages only; the
    old page is kept in .trash/)."""
    g = request.app.state.kb_graph
    first = body.text.strip().splitlines()[0][:200]
    try:
        removed = g.delete_blocks(body.page, first)
    except (KeyError, ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e
    if not removed:
        raise HTTPException(404, "no such memory")
    if body.new and body.new.strip():
        g.write_jarvis_page(body.page, f"{body.new.strip()}\n  learned:: [[{_today_title(g)}]]\n  via:: edited by you")
    await request.app.state.kb_index.sync()
    return {"ok": True, "removed": removed}


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
        heard = await request.app.state.voice.transcribe(data)
    except VoiceError as e:
        raise HTTPException(502, str(e)) from e
    text = request.app.state.vocab.correct(heard)  # names Whisper misheard
    return {"text": text, **({"heard": heard} if text != heard else {})}


class SpeechBody(BaseModel):
    input: str
    model: str | None = None
    voice: str | None = None


@router.post("/audio/speech")
async def speech(body: SpeechBody, request: Request,
                 source: Source = Depends(require_source)) -> Response:
    if not body.input.strip():
        raise HTTPException(400, "empty input")
    spoken = to_speech(body.input[:6000])
    if source is Source.SATELLITE:  # heard, never seen: times, dates and numbers as you'd say them
        spoken = for_ear(spoken)
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


@router.post("/skills/{name:path}/promote")
async def skills_promote(name: str, body: EnableBody, request: Request,
                         _: Source = Depends(require_approver)) -> dict:
    """enabled=true: take the skill out of dry-run, so it really acts; false: back to dry-run."""
    try:
        return request.app.state.skills.promote(name, body.enabled)
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
    recent = sorted(s.latency)
    return {"model": s.llm.model, "models": s.settings.llm_choices(), "loaded": await s.llm.loaded(),
            "median_s": recent[len(recent) // 2] if recent else None}


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


# ---------- watchers, timers and schedules (the HUD's watch panel) ----------
# Watchers are created, changed and removed only here, by you in the HUD: the model has no tool for it.
class WatcherBody(BaseModel):
    watcher: dict
    accept_unverified: bool = False  # you read the warning: the command isn't proven look-only
    replaces: str = ""


class DraftBody(BaseModel):
    description: str
    current: dict | None = None  # the watcher being edited, to refine rather than start over


class NotifyBody(BaseModel):
    title: str = "JARVIS"
    text: str = "Test notification"
    to: list[str] = ["windows", "hud"]


def _bad(res) -> HTTPException:
    return HTTPException(400, str(res))


@router.get("/monitors")
async def monitors(request: Request, _: Source = Depends(require_approver)) -> dict:
    st = request.app.state
    await st.watch.refresh()
    skills = [{"name": s["name"], "schedule": s["schedule"], "enabled": s["enabled"], "dry_run": s.get("dry_run"),
               "last_run": st.skills.last_run.get(s["name"].lower(), 0)}
              for s in st.skills.skills() if s["trigger"] == "schedule"]
    watch_skills = [{"name": s["name"], "match": s["match"], "enabled": s["enabled"]}
                    for s in st.skills.skills() if s["trigger"] == "watch"]
    return {**st.watch.describe(), "timers": st.timers.list(), "schedules": skills, "watch_skills": watch_skills,
            "now": time.time()}


@router.post("/watchers")
async def watcher_save(body: WatcherBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    ok, res = await request.app.state.watch.save(body.watcher, body.accept_unverified, body.replaces)
    if not ok:
        raise _bad(res)
    return res


@router.post("/watchers/test")
async def watcher_test(body: WatcherBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    from jarvis_hub.devices.tools import bridge_call
    ok, res = await bridge_call(request.app.state.settings, "watch_test", {"watcher": body.watcher}, 120)
    if not ok:
        raise _bad(res)
    return res


@router.post("/watchers/{name}/run")
async def watcher_run(name: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    eng = request.app.state.watch
    w = next((w for w in await eng.refresh(force=True) if w["name"] == name), None)
    if not w:
        raise HTTPException(404, "no watcher by that name")
    res = await eng.run(w, manual=True)
    if res.get("error"):
        raise _bad(res["error"])
    return res


@router.post("/watchers/{name}/enable")
async def watcher_enable(name: str, body: EnableBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    ok, res = await request.app.state.watch.set_enabled(name, body.enabled)
    if not ok:
        raise _bad(res)
    return res


@router.delete("/watchers/{name}")
async def watcher_delete(name: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    ok, res = await request.app.state.watch.delete(name)
    if not ok:
        raise _bad(res)
    return {"ok": True}


@router.post("/notify/test")
async def notify_test(body: NotifyBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    return await request.app.state.notifier.send(body.title, body.text, body.to, origin="test")


DRAFT_PROMPT = """You write one "watcher" for a home assistant on a Windows 11 laptop. A watcher is a look-only command
that runs every few minutes; it prints JSON, and items not seen before notify the user.

Reply with ONE JSON object and nothing else, with these fields:
  name: lower-case-with-dashes, 2-41 characters
  description: one sentence, what it watches
  every: like "5m" or "1h" (at least 1m; pick something polite for the API, usually 5m-15m)
  shell: "pwsh" (PowerShell 7, default) or "powershell" (Windows PowerShell 5.1)
  command: one PowerShell command line that PRINTS JSON: an array of objects (or one object). Prefer the
           tool's own JSON output (gh --json, az -o json); otherwise end with | ConvertTo-Json -Compress.
           It must only look: never write, move or delete files, no redirection (>), no Invoke-Expression,
           no nested cmd/powershell, no -EncodedCommand.
  key: the field that identifies one item (so the same item never notifies twice), e.g. "url", "id", "number"
  title: short notification title
  text: notification text, a template with {field} placeholders (dots for nested: {repository.name})
  url: optional link template, e.g. "{url}"
  notify: any of "windows", "android", "hud", "voice" (default ["windows", "hud"])
  env: names of environment variables the command needs (e.g. a PAT the user keeps in .env), else []

What this laptop has: the GitHub CLI gh (logged in; it may hold several accounts: a personal one can be
used with $env:GH_TOKEN = (gh auth token --user NAME); prefer `gh search prs` / `gh pr list --repo`),
the Azure CLI az with the azure-devops extension (az pipelines runs list --org URL --project NAME -o json),
classic Outlook through COM when it is running (New-Object -ComObject Outlook.Application). Use
placeholders like <your-org> only when the request doesn't say; never invent tokens or secrets.

The user's request: """


@router.post("/watchers/draft")
async def watcher_draft(body: DraftBody, request: Request, source: Source = Depends(require_approver)) -> dict:
    """You asked Claude (pressing the button is the approval) to draft a watcher; it's tested, not saved."""
    st = request.app.state
    s = st.settings
    token = s.expert_bridge_token.get_secret_value()
    if not token or token == "change-me":
        raise HTTPException(400, "the expert bridge isn't configured (EXPERT_BRIDGE_TOKEN)")
    question = DRAFT_PROMPT + body.description.strip()[:1500]
    if body.current:
        question += "\n\nRefine this current version rather than starting over:\n" + json.dumps(body.current)[:3000]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(s.expert_timeout_s + 15, connect=5)) as c:
            r = await c.post(s.expert_bridge_url.rstrip("/") + "/v1/ask",
                             json={"question": question, "context": "", "engine": "claude"},
                             headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"the expert bridge is not reachable ({type(e).__name__})")
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    answer = str(data.get("answer", ""))
    st.audit.record(request_id=uuid.uuid4().hex, source=source, tool="ask_claude", risk="external",
                    arguments={"question": question[:1500]}, status="ok" if r.status_code == 200 else "error",
                    approved_by=f"{source.value} (watcher draft)", result=answer[:2000])
    if r.status_code != 200:
        raise HTTPException(502, f"Claude unavailable: {data.get('detail') or r.status_code}")
    start, end = answer.find("{"), answer.rfind("}")
    try:
        draft = json.loads(answer[start:end + 1])
    except ValueError:
        raise HTTPException(502, f"Claude didn't return a watcher: {answer[:300]}")
    from jarvis_hub.devices.tools import bridge_call
    ok, res = await bridge_call(s, "watch_test", {"watcher": draft}, 120)
    return {"draft": draft, "test": res if ok else None, "test_error": None if ok else str(res),
            "model": data.get("model"), "seconds": data.get("seconds")}
