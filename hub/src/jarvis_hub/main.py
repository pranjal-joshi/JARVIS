import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jarvis_hub import __version__
from jarvis_hub.agent.base import EchoAgent
from jarvis_hub.agent.tool_agent import ToolAgent
from jarvis_hub.api import control, events_ws, openai_compat
from jarvis_hub.approval.gate import ApprovalGate
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.config import get_settings
from jarvis_hub.events.bus import EventBus
from jarvis_hub.kb.graph import LogseqGraph
from jarvis_hub.kb.index import KBIndex
from jarvis_hub.expert.tools import register_expert_tool
from jarvis_hub.kb.tools import register_kb_tools, write_daily_summary
from jarvis_hub.llm.client import LLMClient
from jarvis_hub.mcp.registry import ToolRegistry
from jarvis_hub.voice.service import VoiceService

log = logging.getLogger("jarvis_hub")


async def _kb_sync_loop(index: KBIndex, every_s: int) -> None:
    while True:
        try:
            log.info("kb sync: %s", await index.sync())
        except Exception:
            log.exception("kb sync failed")
        await asyncio.sleep(every_s)


async def _daily_summary_loop(graph: LogseqGraph, audit: AuditStore, index: KBIndex) -> None:
    """Write jarvis/daily/<date> at 23:55 local time every day."""
    while True:
        now = datetime.now()
        target = now.replace(hour=23, minute=55, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            write_daily_summary(graph, audit, date.today())
            await index.sync()
        except Exception:
            log.exception("daily summary failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.hub_log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    st = app.state
    st.settings = s
    st.bus = EventBus()
    st.audit = AuditStore(s.hub_db_path)
    st.gate = ApprovalGate(st.bus, s.approval_timeout_s)
    st.tools = ToolRegistry()
    st.llm = LLMClient(s.llm_base_url, s.llm_api_key.get_secret_value(), s.llm_model,
                       s.llm_timeout_s, s.llm_extra())
    embedder = LLMClient(s.embed_base_url, s.llm_api_key.get_secret_value(), s.embed_model)
    st.kb_graph = LogseqGraph(s.kb_graph_dir, s.kb_graph_name)
    st.kb_index = KBIndex(st.kb_graph, embedder, s.embed_model, s.qdrant_url)
    register_kb_tools(st.tools, st.kb_graph, st.kb_index)
    if register_expert_tool(st.tools, s):
        log.info("ask_expert enabled via %s", s.expert_bridge_url)
    await st.tools.load_mcp(s.mcp_config)
    st.voice = VoiceService(
        whisper_uri=s.whisper_uri, piper_uri=s.piper_uri, engine=s.tts_engine,
        voicestudio_url=s.voicestudio_url,
        voicestudio_key=s.voicestudio_api_key.get_secret_value(),
        voicestudio_model=s.voicestudio_tts_model, voicestudio_voice=s.voicestudio_voice,
        language=s.stt_language)
    st.agent = EchoAgent() if s.agent == "echo" else ToolAgent(st.llm, st.tools, st.gate, st.audit)

    tasks = []
    if s.agent != "echo":
        tasks.append(asyncio.create_task(_kb_sync_loop(st.kb_index, s.kb_sync_interval_s)))
        tasks.append(asyncio.create_task(_daily_summary_loop(st.kb_graph, st.audit, st.kb_index)))
    log.info("hub %s ready: agent=%s model=%s tools=%d", __version__, s.agent, s.llm_model,
             len(st.tools.all()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
        await st.tools.aclose()
        await st.llm.aclose()
        await embedder.aclose()
        await st.kb_index.aclose()
        await st.voice.aclose()
        st.audit.close()


app = FastAPI(title="JARVIS hub", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
if origins := [o.strip() for o in get_settings().hub_cors_origins.split(",") if o.strip()]:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["*"])
app.include_router(openai_compat.router)
app.include_router(control.router)
app.include_router(events_ws.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}
