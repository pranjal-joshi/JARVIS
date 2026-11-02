import json
from enum import StrEnum
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Source(StrEnum):
    """Where a request came from. Replies route only back to this source."""

    SATELLITE = "satellite"
    WEB = "web"
    PHONE = "phone"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hub_token_satellite: SecretStr | None = None
    hub_token_web: SecretStr | None = None
    hub_token_phone: SecretStr | None = None
    hub_db_path: str = "jarvis.db"
    hub_log_level: str = "info"
    # Browser origins allowed to call the hub directly (the UI proxies, so usually none).
    hub_cors_origins: str = ""

    # "echo" runs without an LLM (tests, smoke checks); "tools" is the real agent.
    agent: str = "tools"
    llm_base_url: str = "http://ollama:11434/v1"
    llm_api_key: SecretStr = SecretStr("ollama")
    llm_model: str = "qwen3.5:4b"
    # models the HUD may switch between at runtime (comma list); llm_model is always included
    llm_models: str = ""
    llm_timeout_s: float = 180
    # JSON merged into every chat request, e.g. {"reasoning_effort": "none"}
    llm_extra_body: str = "{}"
    # per-model overrides of llm_extra_body, JSON {"model": {...}} (e.g. thinking on for one model)
    llm_model_extra: str = "{}"
    approval_timeout_s: float = 90
    # Claude models (ids starting "claude-") go to the Anthropic API instead of llm_base_url.
    # Empty key = Claude models are hidden from the model picker and never used.
    anthropic_api_key: SecretStr = SecretStr("")
    claude_effort: str = "low"  # low keeps spoken replies quick; medium/high think longer
    # risk levels that wait for approval (comma list of read, write, external, destructive);
    # everything else runs straight away and is audited
    approval_risks: str = "destructive,external"
    # tools the model never sees or may call (internal names, comma list): free-form control of the phone
    model_hidden_tools: str = "adb_tap,adb_type,adb_shell,adb_install,adb_uninstall,adb_connect"
    # local models get only this many request-relevant tools (plus a few always-on ones); 0 = all tools.
    # Claude models always get every visible tool.
    tool_select_k: int = 6
    # the local model's context window (OLLAMA_CONTEXT_LENGTH): conversations are trimmed to fit; 0 = off
    llm_context_tokens: int = 8192
    # notes scoring below this are not returned by search_notes (nomic cosine)
    kb_min_score: float = 0.6
    # where the user lives, for "weather at home"; empty = the first place on the jarvis/location page
    home_place: str = ""
    # requests like "what time is it" or "set a timer" answered by code without the model
    fast_paths: bool = True

    mcp_config: str = "../mcp/servers.yaml"
    # what may run without a human: rules, budgets, grants (guard/policy.py). Missing = APPROVAL_RISKS only.
    policy_file: str = "../policy/policy.yaml"

    kb_graph_dir: str = "../kb/sample-graph"
    kb_graph_name: str = ""  # defaults to the folder name
    kb_sync_interval_s: int = 300
    qdrant_url: str = "http://qdrant:6333"
    embed_base_url: str = "http://ollama:11434/v1"
    embed_model: str = "nomic-embed-text"

    whisper_uri: str = "tcp://whisper:10300"
    piper_uri: str = "tcp://piper:10200"
    stt_language: str = "en"
    tts_engine: str = "auto"
    voicestudio_url: str = "http://voicestudio:3900/v1"
    voicestudio_api_key: SecretStr = SecretStr("")
    voicestudio_tts_model: str = "tts-1"
    voicestudio_voice: str = "jarvis"

    # expert bridge (bridge/expert_bridge.py on the host): empty token = ask_claude/ask_opencode tool off
    expert_bridge_url: str = "http://host.docker.internal:8790"
    expert_bridge_token: SecretStr = SecretStr("")
    expert_default_engine: str = "claude"
    expert_timeout_s: float = 170
    # device bridge (bridge/device_bridge.py on the host): empty token = device tools off
    device_bridge_url: str = "http://host.docker.internal:8791"
    device_bridge_token: SecretStr = SecretStr("")
    # web search (Firecrawl, free plan): empty key = web_search tool off
    firecrawl_api_key: SecretStr = SecretStr("")
    web_search_approval: bool = True  # the query leaves this machine: ask each time unless set false

    # voice satellites
    voice_followup_s: float = 120  # a satellite's last turns are kept this long for follow-ups
    # approval risks a satellite user may give by saying "yes" (the rest wait for the HUD or phone)
    voice_confirm_risks: str = "external"
    # what satellites announce unasked: timer, skill, call (background answers are always announced)
    voice_announce: str = "timer,skill,call"
    # Home Assistant REST (optional): HA_URL defaults to HA_MCP_URL without /api/mcp
    ha_url: str = ""
    ha_mcp_url: str = ""
    ha_token: SecretStr = SecretStr("")
    ha_notify_service: str = ""  # e.g. mobile_app_pixel_9a: approval requests pushed to the phone
    ha_announce_entity: str = ""  # e.g. assist_satellite.home_voice: announcements spoken there
    hud_url: str = ""  # opened by the approval notification, e.g. https://192.168.1.20:3443

    def token_sources(self) -> dict[str, Source]:
        """Map each configured bearer token to the source it may speak for.
        Empty or placeholder tokens are treated as disabled."""
        pairs = {
            Source.SATELLITE: self.hub_token_satellite,
            Source.WEB: self.hub_token_web,
            Source.PHONE: self.hub_token_phone,
        }
        return {
            tok.get_secret_value(): src
            for src, tok in pairs.items()
            if tok and tok.get_secret_value() not in ("", "change-me")
        }

    def ask_for(self) -> frozenset:
        from jarvis_hub.approval.gate import Risk
        return frozenset(Risk(r.strip()) for r in self.approval_risks.split(",") if r.strip())

    def hidden_tools(self) -> frozenset[str]:
        from jarvis_hub.tools.names import internal_name
        return frozenset(internal_name(t.strip()) for t in self.model_hidden_tools.split(",") if t.strip())

    def voice_confirm(self) -> frozenset:
        from jarvis_hub.approval.gate import Risk
        return frozenset(Risk(r.strip()) for r in self.voice_confirm_risks.split(",") if r.strip())

    def ha_base_url(self) -> str:
        url = self.ha_url or self.ha_mcp_url.removesuffix("/").removesuffix("/api/mcp")
        return url.rstrip("/")

    def llm_extra(self) -> dict:
        return json.loads(self.llm_extra_body or "{}")

    def llm_per_model(self) -> dict:
        return json.loads(self.llm_model_extra or "{}")

    def claude_ready(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())

    def llm_choices(self) -> list[str]:
        extra = [m.strip() for m in self.llm_models.split(",") if m.strip()]
        models = list(dict.fromkeys([self.llm_model, *extra]))
        return [m for m in models if not m.startswith("claude-") or self.claude_ready()]

    def default_llm(self) -> str:
        """LLM_MODEL, or the first local choice when it names Claude but no API key is set."""
        choices = self.llm_choices()
        return self.llm_model if self.llm_model in choices else (choices[0] if choices else self.llm_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
