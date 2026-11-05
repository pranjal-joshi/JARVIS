import asyncio
from collections import deque
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jarvis_hub import __version__
from jarvis_hub.agent.base import EchoAgent
from jarvis_hub.agent.fastpath import FastPaths
from jarvis_hub.agent.tool_select import ToolSelector
from jarvis_hub.agent.tool_agent import ToolAgent
from jarvis_hub.api import control, events_ws, hooks, openai_compat
from jarvis_hub.api.hooks import Webhooks
from jarvis_hub.approval.gate import ApprovalGate
from jarvis_hub.audit.store import AuditStore
from jarvis_hub.audit.traces import TraceStore
from jarvis_hub.config import get_settings
from jarvis_hub.events.bus import EventBus
from jarvis_hub.guard.policy import Policy
from jarvis_hub.kb.graph import LogseqGraph
from jarvis_hub.kb.index import KBIndex
from jarvis_hub.automation.skill_tools import register_skill_tools
from jarvis_hub.automation.skills import SkillEngine
from jarvis_hub.devices.tools import register_device_tools
from jarvis_hub.expert.jobs import Jobs, register_job_tools
from jarvis_hub.expert.tools import register_expert_tool
from jarvis_hub.web.search import register_web_search
from jarvis_hub.kb.dreaming import Dreamer, register_dream_tool
from jarvis_hub.kb.tools import register_kb_tools
from jarvis_hub.automation.notify import Notifier, register_notify_tool
from jarvis_hub.automation.watchers import WatchEngine
from jarvis_hub.tools.reminders import Reminders, register_reminder_tools
from jarvis_hub.tools.utility import register_utility_tools
from jarvis_hub.llm.client import LLMClient
from jarvis_hub.mcp.registry import ToolRegistry
from jarvis_hub.voice.announce import Announcer
from jarvis_hub.voice.service import VoiceService
from jarvis_hub.voice.session import Sessions
from jarvis_hub.voice.vocab import Vocabulary

log = logging.getLogger("jarvis_hub")


async def _kb_sync_loop(index: KBIndex, every_s: int) -> None:
    while True:
        try:
            log.info("kb sync: %s", await index.sync())
        except Exception:
            log.exception("kb sync failed")
        await asyncio.sleep(every_s)


async def _warm(llm: LLMClient) -> None:
    """Load the local model now, so the first request doesn't pay ~13 s of loading."""
    if llm.model.startswith("claude-"):
        return
    try:
        await llm.chat([{"role": "user", "content": "ok"}])
        log.info("model %s loaded", llm.model)
    except Exception as e:
        log.info("warm-up of %s failed: %s", llm.model, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.hub_log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    st = app.state
    st.settings = s
    st.bus = EventBus()
    st.audit = AuditStore(s.hub_db_path)
    st.traces = TraceStore(str(Path(s.hub_db_path).with_name("traces.db")), s.trace_days)
    st.gate = ApprovalGate(st.bus, s.approval_timeout_s, s.hub_db_path)
    st.policy = Policy(s.policy_file, s.ask_for(), s.hub_db_path)
    st.tools = ToolRegistry()
    st.llm = LLMClient(s.llm_base_url, s.llm_api_key.get_secret_value(), s.default_llm(),
                       s.llm_timeout_s, s.llm_extra(), s.llm_per_model(), s.llm_endpoints_map())
    if s.claude_ready():
        st.llm.use_claude(s.anthropic_api_key.get_secret_value(), s.claude_effort)
    # the model picked in the HUD survives restarts (only if still allowed)
    st.llm_choice_file = Path(s.hub_db_path).with_name("llm_model.txt")
    if st.llm_choice_file.exists():
        saved = st.llm_choice_file.read_text().strip()
        if saved in s.llm_choices():
            st.llm.model = saved
    embedder = LLMClient(s.embed_base_url, s.llm_api_key.get_secret_value(), s.embed_model)
    st.kb_graph = LogseqGraph(s.kb_graph_dir, s.kb_graph_name)
    st.kb_index = KBIndex(st.kb_graph, embedder, s.embed_model, s.qdrant_url, s.kb_min_score)
    register_kb_tools(st.tools, st.kb_graph, st.kb_index)
    st.timers = register_utility_tools(st.tools, st.kb_graph, st.kb_index,
                                       Path(s.hub_db_path).with_name("timers.json"))
    if register_expert_tool(st.tools, s):
        log.info("ask_claude/ask_opencode enabled via %s", s.expert_bridge_url)
    if register_device_tools(st.tools, s):
        log.info("device tools enabled via %s", s.device_bridge_url)
    if register_web_search(st.tools, s):
        log.info("web_search enabled (approval %s)", "on" if s.web_search_approval else "off")
    await st.tools.load_mcp(s.mcp_config)
    st.voice = VoiceService(
        whisper_uri=s.whisper_uri, piper_uri=s.piper_uri, engine=s.tts_engine,
        voicestudio_url=s.voicestudio_url,
        voicestudio_key=s.voicestudio_api_key.get_secret_value(),
        voicestudio_model=s.voicestudio_tts_model, voicestudio_voice=s.voicestudio_voice,
        language=s.stt_language)
    st.sessions = Sessions(s.voice_followup_s)
    st.vocab = Vocabulary(st.kb_graph, st.audit)
    st.announcer = Announcer(st.bus, s)
    st.notifier = Notifier(st.bus, s, st.announcer)
    register_notify_tool(st.tools, st.notifier)
    st.reminders = Reminders(Path(s.hub_db_path).with_name("reminders.json"), st.notifier, st.bus)
    register_reminder_tools(st.tools, st.reminders)
    st.jobs = Jobs(s, st.kb_graph, st.notifier, st.bus, Path(s.hub_db_path).with_name("jobs_seen.json"))
    st.jobs.on_change = lambda: asyncio.get_running_loop().create_task(st.kb_index.sync())
    if register_job_tools(st.tools, st.jobs):
        log.info("background jobs enabled via %s", s.expert_bridge_url)
    st.latency = deque(maxlen=20)  # seconds per model-answered reply, for the HUD's model badge
    st.bus.tap(lambda ev: ev.type == "trace" and ev.data.get("route") == "model" and not ev.data.get("dry_run")
               and st.latency.append(ev.data["seconds"]))
    st.agent = EchoAgent() if s.agent == "echo" else ToolAgent(
        st.llm, st.tools, st.gate, st.audit, s.ask_for(), hidden=s.hidden_tools(),
        selector=ToolSelector(embedder, s.embed_model, s.tool_select_k),
        sessions=st.sessions, voice_confirm=s.voice_confirm(), context_tokens=s.llm_context_tokens,
        fast=FastPaths(st.kb_graph, s.home_place) if s.fast_paths else None, policy=st.policy,
        traces=st.traces, fallback=s.llm_fallback_model if s.llm_fallback_model in s.llm_choices() else "",
        on_switch=st.llm_choice_file.write_text, remote_context_tokens=s.llm_remote_context_tokens)
    if isinstance(st.agent, ToolAgent):
        st.agent._bus = st.bus
        st.gate.on_orphan = st.agent.run_orphan  # approvals left over from before a restart

    st.skills = SkillEngine(st.kb_graph, st.bus, st.agent, s, Path(s.hub_db_path).with_name("skills.json"))
    register_skill_tools(st.tools, st.skills, st.kb_graph)
    st.hooks = Webhooks(s, Path(s.hub_db_path).with_name("webhooks.json"))
    st.watch = WatchEngine(st.bus, s, st.notifier, st.skills, Path(s.hub_db_path).with_name("watchers_state.json"))
    if s.agent != "echo":
        st.skills.start()
        st.watch.start()
        st.reminders.start()
        if st.tools.get("start_job"):
            st.jobs.start_polling()

    st.dreamer = Dreamer(st.kb_graph, st.traces, st.llm, Path(s.hub_db_path).with_name("dreams.json"),
                         s.dream_at, s.dream_days, st.bus,
                         on_change=lambda: asyncio.get_running_loop().create_task(st.kb_index.sync()),
                         model=s.dream_model if s.dream_model in s.llm_choices() else "")
    register_dream_tool(st.tools, st.dreamer)
    if isinstance(st.agent, ToolAgent) and s.dream_context:
        st.agent.memory = st.dreamer.context
    if s.agent != "echo" and s.dream_enabled:
        st.dreamer.start()

    if n := st.timers.restore(st.bus):
        log.info("%d timers re-armed from before the restart", n)

    tasks = []
    if s.agent != "echo":
        tasks.append(asyncio.create_task(_warm(st.llm)))
        tasks.append(asyncio.create_task(_kb_sync_loop(st.kb_index, s.kb_sync_interval_s)))
    log.info("hub %s ready: agent=%s model=%s tools=%d", __version__, s.agent, s.llm_model,
             len(st.tools.all()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
        await st.skills.stop()
        await st.dreamer.stop()
        await st.watch.stop()
        await st.reminders.stop()
        await st.jobs.stop()
        await st.tools.aclose()
        await st.llm.aclose()
        await embedder.aclose()
        await st.kb_index.aclose()
        await st.voice.aclose()
        st.audit.close()
        st.traces.close()


app = FastAPI(title="JARVIS hub", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
if origins := [o.strip() for o in get_settings().hub_cors_origins.split(",") if o.strip()]:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["*"])
app.include_router(openai_compat.router)
app.include_router(control.router)
app.include_router(hooks.router)
app.include_router(events_ws.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}
