"""Check the SFT files against LFM3's own chat template.

    uv run --no-project --with jinja2 python scripts/sft/check_sft.py TEMPLATE.jinja [DIR]

TEMPLATE.jinja is LFM3's chat template (ollama show jarvis-lfm3-gpu --template > lfm3.jinja, or the
tokenizer_config.json "chat_template" of LiquidAI/LFM3-1.7B). For every sample it checks that:
  - the structured file (messages + tools + tool_calls) renders through the template without error;
  - the text file (LFM3's syntax already in the strings) renders to exactly the same text;
  - the text file rendered as plain ChatML (no LFM3 template, as a generic trainer might) is the same too;
  - every tool call names a tool in that sample's tool list, with only parameters its schema has.
"""

import json
import re
import sys
from pathlib import Path

import jinja2


def env() -> jinja2.Environment:
    e = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)

    def raise_exception(msg):
        raise jinja2.TemplateError(msg)
    e.globals["raise_exception"] = raise_exception

    # transformers' own tojson (what Unsloth's apply_chat_template uses): plain json.dumps, no HTML escaping;
    # jinja2's built-in one writes ' as '
    def tojson(x, ensure_ascii=False, indent=None, separators=None, sort_keys=False):
        return json.dumps(x, ensure_ascii=ensure_ascii, indent=indent, separators=separators, sort_keys=sort_keys)
    e.filters["tojson"] = tojson
    return e


def chatml(messages: list[dict]) -> str:
    return "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages)


def main() -> int:
    template_text = Path(sys.argv[1]).read_text(encoding="utf-8")
    # {% generation %} marks the tokens a trainer learns from (transformers' extension); plain jinja2 lacks it
    template_text = re.sub(r"\{%-?\s*(?:end)?generation\s*-?%\}", "", template_text)
    tpl = env().from_string(template_text)
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/testing/sft")
    problems, n = [], 0
    for suffix in ("", "_val"):
        structured = [json.loads(l) for l in (out / f"lfm3_tools_openai{suffix}.jsonl").read_text(encoding="utf-8").splitlines()]
        text = [json.loads(l) for l in (out / f"lfm3_tools_chatml{suffix}.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(structured) == len(text)
        for i, (s, t) in enumerate(zip(structured, text)):
            n += 1
            where = f"{suffix or 'train'}#{i}"
            try:
                a = tpl.render(messages=s["messages"], tools=s["tools"], bos_token="<|startoftext|>")
                b = tpl.render(messages=t["messages"], tools=None, bos_token="<|startoftext|>")
            except jinja2.TemplateError as e:
                problems.append(f"{where}: template error: {e}")
                continue
            if a != b:
                k = next(j for j in range(min(len(a), len(b))) if a[j] != b[j]) if a[:len(b)] != b[:len(a)] else min(len(a), len(b))
                problems.append(f"{where}: structured and text render differently at {k}: {a[k-40:k+40]!r} vs {b[k-40:k+40]!r}")
            plain = "<|startoftext|>" + chatml(t["messages"])
            if plain != b:
                k = next((j for j in range(min(len(plain), len(b))) if plain[j] != b[j]), min(len(plain), len(b)))
                problems.append(f"{where}: generic ChatML differs at {k}: {plain[k-40:k+40]!r} vs {b[k-40:k+40]!r}")
            names = {x["function"]["name"]: x["function"] for x in s["tools"]}
            for m in s["messages"]:
                for c in m.get("tool_calls") or []:
                    fn = names.get(c["function"]["name"])
                    if fn is None:
                        problems.append(f"{where}: calls {c['function']['name']}, not in its tools")
                        continue
                    allowed = set((fn.get("parameters") or {}).get("properties", {}))
                    extra = set(c["function"]["arguments"]) - allowed
                    if extra:
                        problems.append(f"{where}: {c['function']['name']} has parameters not in its schema: {sorted(extra)}")
            if s["messages"][-1]["role"] != "assistant":
                problems.append(f"{where}: doesn't end on an assistant turn")
    print(f"{n} samples checked, {len(problems)} problems")
    for p in problems[:40]:
        print(" -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
