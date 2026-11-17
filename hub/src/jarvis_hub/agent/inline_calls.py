"""Tool calls a small model wrote as text instead of through the tools interface.

qwen3.5:2b sometimes answers with `<tool_call>{...}</tool_call>`, a bare `{"name": ..., "arguments": ...}`, or the
`<function=name><parameter=k>v</parameter></function>` form, and Ollama hands that back as the reply's text. The
call is usually right; only its transport is wrong. parse_inline reads it back so the agent can run it as a real
call (through the same gate, policy, taint checks and audit) instead of spending a model call on a nudge.

Only the model's own reply is parsed, never a tool result. Anything partial or ambiguous is None: the nudge path
stays the fallback."""

import json
import re

_TAGGED = re.compile(r"<(tool_call|function_call)>\s*(.*?)\s*(?:</\1>|$)", re.S | re.I)
_FUNCTION = re.compile(r"<function=([\w.\-]+)>\s*(.*?)\s*(?:</function>|(?=<function=)|$)", re.S | re.I)
_PARAM = re.compile(r"<parameter=([\w.\-]+)>\s*(.*?)\s*</parameter>", re.S | re.I)
# a reply that starts as a bare call: held back from the stream, and a fake if it can't be used
BARE_CALL = re.compile(r'^\s*\{\s*"(?:name|function)"\s*:', re.S)


def _call(obj) -> dict | None:
    """{"name", "arguments": dict} from one decoded object, or None if it isn't a call."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # {"function": {"name": ..., "arguments": ...}}
        obj = obj["function"]
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    if not any(k in obj for k in ("arguments", "parameters", "args")):
        return None
    args = next(obj[k] for k in ("arguments", "parameters", "args") if k in obj)
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            return None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    return {"name": name.strip(), "arguments": args}


def _objects(text: str) -> list:
    """Every top-level JSON object in text, in order."""
    out, dec, i = [], json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        out.append(obj)
        i = end
    return out


def _param(value: str):
    """A <parameter> value: JSON when it is (a number, a list), else the text."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_inline(text: str) -> list[dict] | None:
    """The tool calls written in text, or None when there are none or any of them can't be read whole."""
    if not text or ("{" not in text and "<function" not in text.lower()):
        return None
    calls: list[dict] = []
    tagged = _TAGGED.findall(text)
    if tagged:
        for _, body in tagged:
            objs = _objects(body)
            if len(objs) != 1 or (c := _call(objs[0])) is None:
                return None
            calls.append(c)
        return calls
    functions = _FUNCTION.findall(text)
    if functions:
        for name, body in functions:
            if body.lstrip().startswith("{"):
                objs = _objects(body)
                if len(objs) != 1 or not isinstance(objs[0], dict):
                    return None
                args = objs[0]
            else:
                args = {k: _param(v) for k, v in _PARAM.findall(body)}
                if not args and body.strip():
                    return None  # a body that is neither JSON nor parameters
            calls.append({"name": name, "arguments": args})
        return calls
    for obj in _objects(text):
        if (c := _call(obj)) is not None:
            calls.append(c)
    return calls or None
