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
        body = c.get("/v1/llm", headers=WEB).json()
        assert (body["model"], body["models"]) == ("small", ["small", "big"])
        assert c.post("/v1/llm", json={"model": "nope"}, headers=WEB).status_code == 400
        assert c.post("/v1/llm", json={"model": "big"}, headers=SAT).status_code == 403  # voice can't switch
        assert c.post("/v1/llm", json={"model": "big"}, headers=WEB).json()["model"] == "big"
    with _client(env, tmp_path) as c:  # restart keeps the choice
        assert c.get("/v1/llm", headers=WEB).json()["model"] == "big"


def test_lfm3_gets_the_model_cards_sampling_and_config_still_wins():
    import asyncio

    import httpx

    from jarvis_hub.llm.client import LLMClient, sampling

    assert sampling("jarvis-lfm3-gpu") == {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1}
    assert sampling("qwen3.5:4b") == {}
    seen = []

    def handler(request):
        import json
        seen.append(json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

    for model, per_model in (("jarvis-lfm3-gpu", {}), ("jarvis-lfm3-gpu", {"jarvis-lfm3-gpu": {"temperature": 0.7}}),
                             ("qwen3.5:4b", {})):
        c = LLMClient("http://x/v1", "k", model, per_model=per_model)
        c._http = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
        asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
    assert seen[0]["temperature"] == 0.1 and seen[0]["top_k"] == 50 and seen[0]["repeat_penalty"] == 1.1
    assert seen[1]["temperature"] == 0.7  # LLM_MODEL_EXTRA overrides the defaults
    assert "temperature" not in seen[2] and "top_k" not in seen[2]
