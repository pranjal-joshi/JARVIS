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


def test_a_cloud_tool_call_that_does_not_parse_is_asked_again():
    import httpx
    sent = []

    def answer(request):
        body = __import__("json").loads(request.content)
        sent.append("tools" in body)
        if len(sent) == 1:  # Groq: the model's tool call didn't parse
            return httpx.Response(400, json={"error": {"code": "tool_use_failed", "message": "Failed to call a function"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Done, sir."}}]})

    llm = LLMClient("http://ollama:11434/v1", "ollama", "groq-x",
                    endpoints={"groq-x": {"base_url": "https://api.groq.com/openai/v1", "model": "x"}})
    llm._remote["groq-x"] = (httpx.AsyncClient(base_url="https://api.groq.com/openai/v1",
                                               transport=httpx.MockTransport(answer)), "x")
    tools = [{"type": "function", "function": {"name": "notify", "parameters": {"type": "object"}}}]
    msg = asyncio.run(llm.chat([{"role": "user", "content": "ping me"}], tools))
    assert msg["content"] == "Done, sir." and sent == [True, True]  # asked again, tools and all
