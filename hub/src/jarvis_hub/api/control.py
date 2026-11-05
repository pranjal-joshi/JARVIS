"""Hub endpoints for the UI and phone: approvals, audit, knowledge base, voice, status."""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from jarvis_hub.api.auth import require_source
from jarvis_hub.approval.gate import APPROVER_SOURCES
from jarvis_hub.config import Source
from jarvis_hub.kb.graph import LINK, PROP
from jarvis_hub.kb.tools import _today_title, recent_memory
from jarvis_hub.tools.cards import family_of
from jarvis_hub.voice.service import VoiceError
from jarvis_hub.voice.speakable import for_ear, to_speech
from jarvis_hub.voice.spoken import symbols

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


# ---------- traces (audit/traces.py) ----------
@router.get("/traces")
async def trace_sessions(request: Request, limit: int = 50, _: Source = Depends(require_approver)) -> list:
    return request.app.state.traces.sessions(min(limit, 500))


@router.get("/traces/{session}")
async def trace_session(session: str, request: Request, _: Source = Depends(require_approver)) -> list:
    return request.app.state.traces.session(session)


@router.get("/tools/rank")
async def tools_rank(say: str, request: Request, before: str = "",
                     _: Source = Depends(require_approver)) -> dict:
    """The tools a request would be offered and every tool's match score (scripts/eval/rank_tools.py)."""
    msgs = [{"role": "user", "content": x} for x in (before, say) if x]
    r = await request.app.state.agent.ranked(msgs)
    return {"offered": [t.name for t in r.tools],
            "scores": dict(sorted(r.scores.items(), key=lambda kv: -kv[1]))}


# ---------- knowledge base ----------
@router.get("/kb/search")
async def kb_search(q: str, request: Request, limit: int = 8,
                    _: Source = Depends(require_approver)) -> list:
    return await request.app.state.kb_index.search(q, min(limit, 50))


@router.get("/kb/graph")
async def kb_graph(request: Request, agent: bool = False, _: Source = Depends(require_approver)) -> dict:
    """The KB's pages and links; agent=true adds what JARVIS can do: its tools (grouped by family) and skills."""
    g = request.app.state.kb_index.link_graph()
    return _with_agent(request, g) if agent else g


def _with_agent(request: Request, g: dict) -> dict:
    """Tool nodes ("tool:<name>") under one node per family ("tools/<family>") under "tools"; skill pages marked as
    skills, with their trigger and state, and linked to the tools their instructions name."""
    st = request.app.state
    nodes, edges = {n["id"]: n for n in g["nodes"]}, list(g["edges"])
    hidden, ask = st.settings.hidden_tools(), st.settings.ask_for()
    nodes["tools"] = {"id": "tools", "label": "tools", "kind": "family", "blocks": 0, "journal": False, "jarvis": False}
    tools = [t for t in st.tools.all() if t.name not in hidden]
    for t in tools:
        fam = family_of(t.name) or (t.origin if t.origin != "builtin" else "other")
        fid = f"tools/{fam}"
        if fid not in nodes:
            nodes[fid] = {"id": fid, "label": fam, "kind": "family", "blocks": 0, "journal": False, "jarvis": False}
            edges.append({"source": "tools", "target": fid})
        tid = f"tool:{t.name}"
        nodes[tid] = {"id": tid, "label": t.model_name, "kind": "tool", "blocks": 0, "journal": False,
                      "jarvis": False, "family": fam, "origin": t.origin, "risk": str(t.risk),
                      "asks": t.risk in ask and not t.self_scoped, "description": t.description[:600],
                      "params": sorted((t.parameters or {}).get("properties", {}))}
        edges.append({"source": fid, "target": tid})
    skills = {s["name"].lower(): s for s in st.skills.skills()}
    if skills and "skills" in nodes:
        edges.append({"source": "tools", "target": "skills"})
    for key, s in skills.items():
        if key not in nodes:
            continue
        nodes[key].update(kind="skill", trigger=s["trigger"] or "manual", schedule=s["schedule"],
                          enabled=s["enabled"], dry_run=s["dry_run"])
        said = s["instructions"].lower()
        for t in tools:
            if t.model_name.lower() in said or t.name.lower() in said:
                edges.append({"source": key, "target": f"tool:{t.name}"})
    if "skills" in nodes:
        nodes["skills"]["kind"] = "family"
    return {"nodes": list(nodes.values()), "edges": edges}


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


def _said_and_props(text: str) -> tuple[str, dict]:
    """A block as the reader shows it (without its learned::/via:: lines) and those properties."""
    said, props = [], {}
    for line in text.splitlines():
        if m := PROP.match(line):
            props[m.group(1).lower()] = LINK.sub(r"\1", m.group(2)).strip()
        else:
            said.append(line)
    return "\n".join(said).strip(), props


@router.get("/kb/page")
async def kb_page(name: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    g = request.app.state.kb_graph
    pages = g.pages()
    key = name.strip().lower()
    page = next((p for p in pages if p.name.lower() == key), None)
    if page is None:
        raise HTTPException(404, "no such page")
    blocks = page.blocks[:400]
    shown = [_said_and_props(b.text) for b in blocks]
    return {"name": page.name, "journal": page.is_journal, "link": g.deep_link(page.name),
            "blocks": [b.text for b in blocks],  # as written: sent back to delete, edit or move one
            "index": [b.index for b in blocks],  # each block's place on the page
            "shown": [t for t, _ in shown],  # what each says, without its properties
            "props": [pr for _, pr in shown],  # learned (a day), via (who said it), ...
            "digest": page.digest,  # the page as read (deleting or renaming it is refused once it changed)
            "updated": int(page.path.stat().st_mtime),
            "links": sorted({to for b in page.blocks for to in b.links}),
            "backlinks": sorted(p.name for p in pages if p is not page and key in p.links)}


class BlockDelete(BaseModel):
    page: str
    index: int  # from /kb/page "index"
    text: str  # the block as it was read: a page changed since then is refused


@router.post("/kb/block/delete")
async def kb_block_delete(body: BlockDelete, request: Request, source: Source = Depends(require_approver)) -> dict:
    """The HUD's knowledge map: delete one point (and what is nested under it) from any page. The page as it
    was is kept in .trash/, and the deletion is in the audit log."""
    g = request.app.state.kb_graph
    try:
        g.delete_block(body.page, body.index, body.text)
    except (KeyError, ValueError, PermissionError) as e:
        raise HTTPException(409 if "reload" in str(e) else 400, str(e)) from e
    request.app.state.audit.record(request_id=uuid.uuid4().hex, source=source, tool="kb_block_delete",
                                   risk="destructive", arguments={"page": body.page, "index": body.index},
                                   status="ok", approved_by=f"{source} (by hand)", result=body.text[:500])
    await request.app.state.kb_index.sync()
    return {"ok": True}


class PageDelete(BaseModel):
    page: str
    digest: str  # from /kb/page: a page changed since it was read is refused


@router.post("/kb/page/delete")
async def kb_page_delete(body: PageDelete, request: Request, source: Source = Depends(require_approver)) -> dict:
    """The HUD's knowledge map: delete a whole page (or journal day). It is moved to .trash/, and the deletion
    is in the audit log."""
    g = request.app.state.kb_graph
    page = g.page(body.page)
    if page is None:
        raise HTTPException(404, "no such page")
    if page.digest != body.digest:
        raise HTTPException(409, "the page changed since it was read; reload it")
    try:
        gone = g.trash_page(page.name)
    except (KeyError, ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e
    request.app.state.audit.record(request_id=uuid.uuid4().hex, source=source, tool="kb_page_delete",
                                   risk="destructive", arguments={"page": gone}, status="ok",
                                   approved_by=f"{source} (by hand)", result=f"{len(page.blocks)} points")
    await request.app.state.kb_index.sync()
    return {"ok": True, "page": gone}


class BlockEdit(BaseModel):
    page: str
    index: int
    text: str  # as read: a page changed since then is refused
    new: str


@router.post("/kb/block/edit")
async def kb_block_edit(body: BlockEdit, request: Request, source: Source = Depends(require_approver)) -> dict:
    """The HUD's knowledge map: change what one point says (its learned::/via:: lines stay). The page as it was
    is kept in .trash/."""
    g = request.app.state.kb_graph
    try:
        g.edit_block(body.page, body.index, body.text, body.new)
    except (KeyError, ValueError, PermissionError) as e:
        raise HTTPException(409 if "reload" in str(e) else 400, str(e)) from e
    _by_hand(request, source, "kb_block_edit", "write", {"page": body.page, "index": body.index}, body.new)
    await request.app.state.kb_index.sync()
    return {"ok": True}


class BlockAdd(BaseModel):
    page: str
    text: str


@router.post("/kb/block/add")
async def kb_block_add(body: BlockAdd, request: Request, source: Source = Depends(require_approver)) -> dict:
    """The HUD's knowledge map: a new point at the end of a page (made if it doesn't exist)."""
    if not body.text.strip():
        raise HTTPException(400, "say what the point is")
    try:
        page = request.app.state.kb_graph.add_block(body.page, body.text.strip())
    except (KeyError, ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e
    _by_hand(request, source, "kb_block_add", "write", {"page": page}, body.text)
    await request.app.state.kb_index.sync()
    return {"ok": True, "page": page}


class BlockMove(BaseModel):
    page: str
    index: int
    text: str  # as read: a page changed since then is refused
    to: str


@router.post("/kb/block/move")
async def kb_block_move(body: BlockMove, request: Request, source: Source = Depends(require_approver)) -> dict:
    """The HUD's knowledge map: move one point (with what is nested under it) to the end of another page, made
    if it doesn't exist. The page it left is kept in .trash/ as it was."""
    g = request.app.state.kb_graph
    to = body.to.strip()
    if not to or to.lower() == body.page.strip().lower():
        raise HTTPException(400, "pick another page to move it to")
    try:
        moved = g.take_block(body.page, body.index, body.text)
        to = g.add_block(to, moved)
    except (KeyError, ValueError, PermissionError) as e:
        raise HTTPException(409 if "reload" in str(e) else 400, str(e)) from e
    _by_hand(request, source, "kb_block_move", "write", {"page": body.page, "to": to}, body.text)
    await request.app.state.kb_index.sync()
    return {"ok": True, "to": to}


class PageRename(BaseModel):
    page: str
    digest: str  # from /kb/page
    to: str


@router.post("/kb/page/rename")
async def kb_page_rename(body: PageRename, request: Request, source: Source = Depends(require_approver)) -> dict:
    """The HUD's knowledge map: rename a page; links to it on other pages follow (each kept in .trash/ as it
    was)."""
    g = request.app.state.kb_graph
    page = g.page(body.page)
    if page is None:
        raise HTTPException(404, "no such page")
    if page.digest != body.digest:
        raise HTTPException(409, "the page changed since it was read; reload it")
    try:
        to, changed = g.rename_page(page.name, body.to)
    except (KeyError, ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e
    _by_hand(request, source, "kb_page_rename", "write", {"page": page.name, "to": to}, f"{changed} pages relinked")
    await request.app.state.kb_index.sync()
    return {"ok": True, "page": to, "relinked": changed}


@router.get("/kb/trash")
async def kb_trash(request: Request, limit: int = 100, _: Source = Depends(require_approver)) -> list:
    return request.app.state.kb_graph.trash()[:min(limit, 500)]


class TrashRestore(BaseModel):
    id: str  # from /kb/trash


@router.post("/kb/trash/restore")
async def kb_trash_restore(body: TrashRestore, request: Request, source: Source = Depends(require_approver)) -> dict:
    """Put a page back as it was in .trash/; the page as it is now (if any) goes to .trash/ in its place."""
    try:
        page = request.app.state.kb_graph.restore(body.id)
    except KeyError as e:
        raise HTTPException(404, "no such copy in the trash") from e
    _by_hand(request, source, "kb_trash_restore", "write", {"id": body.id}, page)
    await request.app.state.kb_index.sync()
    return {"ok": True, "page": page}


def _by_hand(request: Request, source: Source, tool: str, risk: str, arguments: dict, result: str) -> None:
    """An edit made by hand in the HUD, in the audit log like a tool's."""
    request.app.state.audit.record(request_id=uuid.uuid4().hex, source=source, tool=tool, risk=risk,
                                   arguments=arguments, status="ok", approved_by=f"{source} (by hand)",
                                   result=result[:500])


@router.get("/dream")
async def dream_state(request: Request, _: Source = Depends(require_approver)) -> dict:
    d = request.app.state.dreamer
    return {"running": d.running, "at": d.at, "days": d.days, "last": d.state.get("last"),
            "context": d.context(), "kept": [{"text": t, **p} for t, p in d.kept()]}


class DreamBody(BaseModel):
    dry_run: bool = False  # only report what it would keep
    days: int | None = None


@router.post("/dream/run")
async def dream_run(body: DreamBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    """Dream now (the HUD's button): the report, and what it would keep with dry_run."""
    from jarvis_hub.kb.dreaming import summary
    d = request.app.state.dreamer
    if d.running:
        raise HTTPException(409, "a dream is already running")
    rep = await d.run(dry_run=body.dry_run, days=body.days, force=True)
    return {**rep.__dict__, "said": summary(rep)}


@router.get("/kb/tasks")
async def kb_tasks(request: Request, _: Source = Depends(require_source)) -> list:
    return [t.__dict__ for t in request.app.state.kb_graph.tasks()]


@router.post("/kb/sync")
async def kb_sync(request: Request, _: Source = Depends(require_approver)) -> dict:
    return await request.app.state.kb_index.sync()


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
    text = symbols(request.app.state.vocab.correct(heard))  # names Whisper misheard; "slash" -> "/"
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
    description: str  # what you want watched, in your words
    current: dict | None = None  # the watcher being edited, to refine rather than start over
    answers: list[dict] = []  # [{question, answer}]: Claude's questions so far and your replies


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
    jobs = st.jobs.cards() if st.tools.get("start_job") else []
    return {**st.watch.describe(), "timers": st.timers.list(), "schedules": skills, "watch_skills": watch_skills,
            # running, not yet dismissed, or ended in the last hours (the HUD shows a card for the first two)
            "jobs": [j for j in jobs if j["state"] in ("queued", "running") or not j["dismissed"]
                     or time.time() - (j.get("ended") or 0) < 6 * 3600][:10],
            "now": time.time()}


@router.post("/jobs/clear")
async def jobs_clear(request: Request, _: Source = Depends(require_approver)) -> dict:
    """Take the finished background jobs off the Monitors list (their reports stay in the notes)."""
    return {"cleared": request.app.state.jobs.clear()}


@router.post("/jobs/{jid}/dismiss")
async def job_dismiss(jid: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    """Close a finished background job's card in the HUD (its report stays in the notes)."""
    if not request.app.state.jobs.dismiss(jid):
        raise HTTPException(400, "no finished job with that id")
    return {"ok": True}


@router.post("/jobs/{jid}/cancel")
async def job_cancel(jid: str, request: Request, _: Source = Depends(require_approver)) -> dict:
    """Stop a running background job (the HUD card's stop button)."""
    ok, data = await request.app.state.jobs.cancel(jid)
    if not ok:
        raise HTTPException(400, str(data.get("detail")))
    return {"ok": True}


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

If something essential is missing and can't be found by a command (which repo or org, an Azure DevOps
organisation URL or project, which mailbox folder...), reply with ONLY {"question": "<one short question>"}.
Ask at most what you need, one question at a time; otherwise never ask.

Else reply with ONE JSON object and nothing else, with these fields:
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


async def _ask_claude_json(st, source: Source, question: str) -> tuple[dict, dict]:
    s = st.settings
    token = s.expert_bridge_token.get_secret_value()
    if not token or token == "change-me":
        raise HTTPException(400, "the expert bridge isn't configured (EXPERT_BRIDGE_TOKEN)")
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
                    arguments={"question": question[-1500:]}, status="ok" if r.status_code == 200 else "error",
                    approved_by=f"{source.value} (watcher draft)", result=answer[:2000])
    if r.status_code != 200:
        raise HTTPException(502, f"Claude unavailable: {data.get('detail') or r.status_code}")
    start, end = answer.find("{"), answer.rfind("}")
    try:
        return json.loads(answer[start:end + 1]), data
    except ValueError:
        raise HTTPException(502, f"Claude didn't return a watcher: {answer[:300]}")


@router.post("/watchers/draft")
async def watcher_draft(body: DraftBody, request: Request, source: Source = Depends(require_approver)) -> dict:
    """You describe it, Claude writes it (pressing the button is the approval). Claude may ask one question
    back; a draft that fails its test goes back to Claude once with the error. Tested here, never saved."""
    from jarvis_hub.devices.tools import bridge_call
    st = request.app.state
    question = DRAFT_PROMPT + body.description.strip()[:1500]
    for qa in body.answers[-5:]:
        question += f"\n\nYou asked: {str(qa.get('question', ''))[:300]}\nThe user answered: {str(qa.get('answer', ''))[:500]}"
    if body.current:
        question += "\n\nChange this current version rather than starting over:\n" + json.dumps(body.current)[:3000]
    draft, data = await _ask_claude_json(st, source, question)
    seconds = float(data.get("seconds") or 0)
    if draft.get("question") and not draft.get("command"):
        return {"question": str(draft["question"])[:300], "model": data.get("model"), "seconds": seconds}
    ok, res = await bridge_call(st.settings, "watch_test", {"watcher": draft}, 120)
    fixed = False
    if not ok:  # one round of repair: Claude sees exactly why its draft failed
        retry = (question + "\n\nYour draft was:\n" + json.dumps(draft)[:3000]
                 + f"\nRunning it failed with: {str(res)[:600]}\nReply with the corrected watcher JSON only.")
        draft2, data2 = await _ask_claude_json(st, source, retry)
        seconds += float(data2.get("seconds") or 0)
        if draft2.get("command"):
            draft, fixed = draft2, True
            ok, res = await bridge_call(st.settings, "watch_test", {"watcher": draft}, 120)
    return {"draft": draft, "test": res if ok else None, "test_error": None if ok else str(res), "fixed": fixed,
            "model": data.get("model"), "seconds": round(seconds, 1)}
