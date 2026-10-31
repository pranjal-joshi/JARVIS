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
    llm_timeout_s: float = 180
    # JSON merged into every chat request, e.g. {"reasoning_effort": "none"}
    llm_extra_body: str = "{}"
    approval_timeout_s: float = 90

    mcp_config: str = "../mcp/servers.yaml"

    kb_graph_dir: str = "../kb/sample-graph"
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

    def llm_extra(self) -> dict:
        return json.loads(self.llm_extra_body or "{}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
