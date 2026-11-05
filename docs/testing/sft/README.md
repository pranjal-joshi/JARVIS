# LFM3 tool-calling SFT data

Built from JARVIS's own traces by [`scripts/sft/build_sft.py`](../../../scripts/sft/build_sft.py) and checked
against LFM3's chat template by [`scripts/sft/check_sft.py`](../../../scripts/sft/check_sft.py).
`report.md` says what went in. The `.jsonl` files are gitignored: they hold your notes and conversations.

## Rebuild

```bash
python -I scripts/sft/build_sft.py
```

That pulls the traces and tool schemas from the running hub container. Turns that no eval case grades need a
verdict in `scripts/sft/review.json`; list the new ones with `--list-unreviewed`. Then check:

```bash
ollama show jarvis-lfm3-gpu --template > lfm3.jinja   # or docker exec jarvis-ollama-1 ollama show ...
uv run --no-project --with jinja2 python -I scripts/sft/check_sft.py lfm3.jinja
```

## Files

| file | shape | use |
|---|---|---|
| `lfm3_tools_chatml.jsonl` / `_val` | `{"messages": [...]}`, plain strings | Unsloth Studio, any ChatML trainer |
| `lfm3_tools_openai.jsonl` / `_val` | `{"messages": [...], "tools": [...]}`, structured `tool_calls` | trainers that call `apply_chat_template(messages, tools=...)` |

Both hold the same samples. In the ChatML file LFM3's own tool syntax is already in the text: the tool list as
`<tools>` JSON lines at the end of the system message, each call as `<tool_call><function=…><parameter=…>` XML
in the assistant turn (after `<think></think>`), each result wrapped in `<tool_response>`. So it renders to the
same tokens through LFM3's template **and** through a generic ChatML one (checked for every sample).

## Unsloth Studio

1. Model: `LiquidAI/LFM3-1.7B` (the Hugging Face weights, not the GGUF).
2. Dataset: upload `lfm3_tools_chatml.jsonl` (and `_val` for evaluation); format **chatml**.
3. Chat template: the model's own. Train on assistant turns only, if offered.
4. Max sequence length 8192 covers every sample (the longest is ~18k characters, about 4.6k tokens; most
   are ~6k characters).
5. It's a small set (about 200 samples): LoRA, 1-3 epochs, and mix in general tool-calling data if the model
   starts forgetting other skills. Afterwards, run `python scripts/eval/run_eval.py --model <new model>`.

Not checked: whether Studio's ChatML loader accepts the `tool` role as is. If it rejects it, use the
`openai` file in a notebook with `tokenizer.apply_chat_template(messages, tools=tools)`.
