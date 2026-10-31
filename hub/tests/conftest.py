import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_hub.config import get_settings

SAMPLE_GRAPH = Path(__file__).resolve().parents[2] / "kb" / "sample-graph"


@pytest.fixture
def graph_dir(tmp_path) -> Path:
    dst = tmp_path / "graph"
    shutil.copytree(SAMPLE_GRAPH, dst)
    return dst


@pytest.fixture
def env(tmp_path, graph_dir, monkeypatch):
    monkeypatch.setenv("HUB_TOKEN_SATELLITE", "sat-token")
    monkeypatch.setenv("HUB_TOKEN_WEB", "web-token")
    monkeypatch.setenv("HUB_TOKEN_PHONE", "change-me")  # placeholder = disabled
    monkeypatch.setenv("HUB_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("KB_GRAPH_DIR", str(graph_dir))
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:9")  # unreachable -> keyword fallback
    monkeypatch.setenv("AGENT", "echo")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def client(env):
    from jarvis_hub.main import app

    with TestClient(app) as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
