"""Claude adapter: OpenAI-style history <-> Messages API turns (no network)."""

from jarvis_hub.config import Settings
from jarvis_hub.llm.claude import _messages, _tools


def test_history_converts_with_tool_round_trip():
    system, turns = _messages([
        {"role": "system", "content": "You are JARVIS."},
        {"role": "user", "content": "lamp?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "ha__state", "arguments": '{"name": "lamp"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "off"},
        {"role": "assistant", "content": "It is off, sir."},
    ])
    assert system == "You are JARVIS."
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "ha__state", "input": {"name": "lamp"}}
    assert turns[2]["content"][0]["type"] == "tool_result" and turns[2]["content"][0]["tool_use_id"] == "t1"


def test_claude_blocks_are_replayed_unchanged():
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"},
           {"type": "tool_use", "id": "t9", "name": "get_time", "input": {}}]
    _, turns = _messages([{"role": "user", "content": "time?"},
                          {"role": "assistant", "content": "", "tool_calls": [], "_claude": raw}])
    assert turns[1]["content"] == raw


def test_tools_convert():
    out = _tools([{"type": "function", "function": {"name": "x", "description": "d", "parameters": {"type": "object"}}}])
    assert out == [{"name": "x", "description": "d", "input_schema": {"type": "object"}}]


def test_claude_hidden_without_a_key():
    s = Settings(llm_model="claude-haiku-5-5", llm_models="jarvis-2b-gpu", anthropic_api_key="")
    assert s.llm_choices() == ["jarvis-2b-gpu"] and s.default_llm() == "jarvis-2b-gpu"
    s = Settings(llm_model="claude-haiku-5-5", llm_models="jarvis-2b-gpu", anthropic_api_key="k")
    assert s.llm_choices() == ["claude-haiku-5-5", "jarvis-2b-gpu"] and s.default_llm() == "claude-haiku-5-5"
