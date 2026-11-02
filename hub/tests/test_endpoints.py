import asyncio

from jarvis_hub.config import Settings
from jarvis_hub.llm.client import LLMClient


def test_remote_models_join_the_picker_and_route_to_their_server():
    s = Settings(llm_model="jarvis-lfm3-gpu", llm_models="",
                 llm_endpoints='{"dgx-27b": {"base_url": "http://host.docker.internal:8001/v1", "model": "qwen-27b"},'
                               ' "broken": {"model": "no-url"}}')
    assert s.llm_choices() == ["jarvis-lfm3-gpu", "dgx-27b"]

    llm = LLMClient("http://ollama:11434/v1", "ollama", "jarvis-lfm3-gpu", endpoints=s.llm_endpoints_map())
    http, model = llm._route()
    assert str(http.base_url).startswith("http://ollama:11434") and model == "jarvis-lfm3-gpu"
    llm.model = "dgx-27b"
    http, model = llm._route()
    assert str(http.base_url).startswith("http://host.docker.internal:8001") and model == "qwen-27b"
    asyncio.run(llm.aclose())
