from fastapi.testclient import TestClient

WEB = {"Authorization": "Bearer web-token"}
SAT = {"Authorization": "Bearer sat-token"}


def _client(env, tmp_path):
    env.setenv("LLM_MODEL", "small")
    env.setenv("LLM_MODELS", "small, big")
    env.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")  # unreachable: warm-up fails quietly
    from jarvis_hub.main import app
    return TestClient(app)


def test_switch_is_allow_listed_and_persists(env, tmp_path):
    with _client(env, tmp_path) as c:
        assert c.get("/v1/llm", headers=WEB).json() == {"model": "small", "models": ["small", "big"]}
        assert c.post("/v1/llm", json={"model": "nope"}, headers=WEB).status_code == 400
        assert c.post("/v1/llm", json={"model": "big"}, headers=SAT).status_code == 403  # voice can't switch
        assert c.post("/v1/llm", json={"model": "big"}, headers=WEB).json()["model"] == "big"
    with _client(env, tmp_path) as c:  # restart keeps the choice
        assert c.get("/v1/llm", headers=WEB).json()["model"] == "big"
