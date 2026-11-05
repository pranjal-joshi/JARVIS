"""Build an SFT dataset for LFM3 tool calling from JARVIS's own traces.

    python scripts/sft/build_sft.py                 # pulls traces + tool schemas from the running hub container
    python scripts/sft/build_sft.py --traces t.json --tools tools.json   # or from files exported before

Writes docs/testing/sft/ (gitignored data, see README there):
  lfm3_tools_chatml.jsonl   {"messages": [...]}: LFM3's tool format written into the text (tools in the system
                            message, <tool_call> XML in the assistant turns, <tool_response> in tool turns). Loads as
                            a plain ChatML dataset (Unsloth Studio: format "chatml") and renders to the same tokens
                            through LFM3's own template or a generic ChatML one.
  lfm3_tools_openai.jsonl   {"messages": [...], "tools": [...]}: the same samples with structured tool_calls
                            (arguments as objects, as LFM3's template requires), for trainers that pass tools=.
  *_val.jsonl               the same, held out (10%, split by request so near-duplicates stay together).
  report.md                 what was kept, repaired and dropped, and why.

Which turns teach the right thing:
  - a turn whose request is an eval case (scripts/eval/cases.json) is graded with the eval's own score(); style
    misses and device-bridge outages don't count against it (the tool call itself was right);
  - any other turn must have no failed or refused call, and a person's verdict in scripts/sft/review.json
    ("no errors" isn't enough: a reminder sent as a one-off notification ran cleanly); --list-unreviewed
    prints the turns still waiting for one;
  - a step the harness nudged ("(system) Your reply says you saved...") is repaired: the wrong attempt and the
    nudge are dropped and the corrected step kept;
  - a refused duplicate call is dropped;
  - a dry-run or bridge-down result isn't a real tool result: the sample ends at the tool call (it still teaches
    which tool, with what arguments).
"""

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "testing" / "sft"
NOW = re.compile(r"\n\n\(Now: [^)]*\)\.?\s*$")
NUDGE = "(system)"
BRIDGE_DOWN = "the device bridge is not reachable"
FAKE = ("(dry run",)
BAD_REPLY = re.compile(r"(?i)couldn't quite catch|didn't catch that|\(dry run\)|would answer here|i ran into a problem")
# the harness's guards refusing a wrong call: the refused step is dropped (a repair), the rest of the turn graded
GUARD = re.compile(r"you already called|didn't ask (?:about a time|you to write)|general knowledge, not current|"
                   r"which the user didn't say|would save words nobody said")
KEEP_PER_KEY = 2  # copies of the same request + tool calls (evals ran each case many times)


def _run_eval_module():
    spec = importlib.util.spec_from_file_location("run_eval", ROOT / "scripts" / "eval" / "run_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pull_from_hub() -> tuple[list[dict], list[dict]]:
    """The traces and every tool's schema (as the model sees it) from the hub container."""
    traces = subprocess.run(["docker", "exec", "-i", "jarvis-hub-1", "python", "-"], input=TRACES_PY, text=True,
                            capture_output=True, check=True, encoding="utf-8").stdout
    tools = subprocess.run(["docker", "exec", "-i", "jarvis-hub-1", "python", "-"], input=TOOLS_PY, text=True,
                           capture_output=True, check=True, encoding="utf-8").stdout
    return json.loads(traces), json.loads(tools)


TRACES_PY = """
import sqlite3, json
c = sqlite3.connect('/data/traces.db')
cols = ("request_id","ts","session","source","route","model","seconds","said","reply","data")
rows = [dict(zip(cols, r)) for r in c.execute("select " + ",".join(cols) + " from turns order by ts")]
for r in rows: r["data"] = json.loads(r["data"])
print(json.dumps(rows))
"""

# every tool registered with stand-in services: nothing is started, called or written
TOOLS_PY = """
import asyncio, json, sys
from unittest.mock import MagicMock as m
from jarvis_hub.config import Settings
from jarvis_hub.mcp.registry import ToolRegistry
from jarvis_hub.automation.skill_tools import register_skill_tools
from jarvis_hub.devices.tools import register_device_tools
from jarvis_hub.expert.tools import register_expert_tool
from jarvis_hub.expert.jobs import Jobs, register_job_tools
from jarvis_hub.events.bus import EventBus
from jarvis_hub.web.search import register_web_search
from jarvis_hub.kb.dreaming import register_dream_tool
from jarvis_hub.kb.tools import register_kb_tools
from jarvis_hub.automation.notify import register_notify_tool
from jarvis_hub.tools.reminders import register_reminder_tools
from jarvis_hub.tools.utility import register_utility_tools
s, reg = Settings(), ToolRegistry()
for f in (lambda: register_kb_tools(reg, m(), m()), lambda: register_utility_tools(reg, m(), m()),
          lambda: register_expert_tool(reg, s), lambda: register_device_tools(reg, s),
          lambda: register_web_search(reg, s), lambda: register_notify_tool(reg, m()),
          lambda: register_reminder_tools(reg, m()), lambda: register_skill_tools(reg, m(), m()),
          lambda: register_dream_tool(reg, m()), lambda: register_job_tools(reg, Jobs(s, m(), None, EventBus()))):
    try:
        f()
    except TypeError as e:
        print("skipped:", e, file=sys.stderr)
print(json.dumps([{**t.openai_schema()["function"], "internal": t.name, "risk": str(t.risk)} for t in reg.all()]))
"""


# ---------------------------------------------------------------- labelling

def said_of(msg: dict) -> str:
    return NOW.sub("", msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else ""


def case_index(cases: list[dict]) -> dict[str, dict]:
    idx = {}
    for c in cases:
        last = c.get("say") or next((t for t in reversed(c.get("turns", [])) if isinstance(t, str)), "")
        idx[last.strip().lower()] = c
    return idx


def turn_calls(row: dict, by_model: dict) -> list[dict]:
    """The calls of a whole turn, shaped like the audit rows run_eval.score() grades."""
    out = []
    for loop in row["data"].get("loops") or []:
        results = {m.get("tool_call_id"): m.get("content") or "" for m in loop["convo"] if m["role"] == "tool"}
        for m in loop["convo"]:
            for tc in m.get("tool_calls") or [] if m["role"] == "assistant" else []:
                name = tc["function"]["name"]
                res = results.get(tc.get("id"), "")
                body = re.sub(r"^<tool_result[^>]*>\s*", "", res)
                status = ("guarded" if body.startswith("ERROR") and GUARD.search(body[:160])
                          else "error" if body.startswith("ERROR") else "ok")
                tool = by_model.get(name, {})
                out.append({"tool": tool.get("internal", name), "status": status, "result": body,
                            "risk": tool.get("risk", "read"), "arguments": tc["function"].get("arguments") or "{}"})
    return out


def grade(row: dict, case: dict | None, calls: list[dict], score) -> tuple[bool, str]:
    """(good, why) for the whole turn, without the calls a guard refused (repair() drops those steps)."""
    calls = [c for c in calls if c["status"] != "guarded"]
    if case is not None:
        fails = [f for f in score(case, {"reply": row.get("reply") or ""}, calls)
                 if not f.startswith(("style:", "reply:")) and BRIDGE_DOWN not in f]
        return (not fails, fails[0] if fails else "eval pass")
    if any(c["status"] == "rejected" for c in calls):
        return False, "refused call"
    if any(c["status"] == "error" and BRIDGE_DOWN not in c["result"] for c in calls):
        return False, "failed call"
    if BAD_REPLY.search(row.get("reply") or "") or not (row.get("reply") or "").strip():
        return False, "unusable reply"
    return True, "clean"


# ---------------------------------------------------------------- one sample per loop (part)

def repair(convo: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop nudged attempts (the attempt, its tool results, the nudge) and refused duplicate calls."""
    fixes, out = [], []
    for m in convo:
        if m["role"] == "user" and str(m.get("content", "")).startswith(NUDGE):
            # back up to the assistant step the nudge was about, dropping it and anything after it
            while out and out[-1]["role"] != "assistant":
                out.pop()
            if out and out[-1]["role"] == "assistant":
                out.pop()
            fixes.append("nudge: " + m["content"][9:60])
            continue
        out.append(m)
    # calls a guard refused (a duplicate, a time or search nobody asked for, words nobody said): the step and results
    results = {m.get("tool_call_id"): re.sub(r"^<tool_result[^>]*>\s*", "", str(m.get("content", "")))
               for m in out if m["role"] == "tool"}
    refused = {k for k, v in results.items() if v.startswith("ERROR") and GUARD.search(v[:160])}
    keep = []
    for m in out:
        calls = m.get("tool_calls") or []
        if m["role"] == "assistant" and calls and all(c.get("id") in refused for c in calls):
            fixes.append("guard-refused call")
            continue
        if m["role"] == "tool" and m.get("tool_call_id") in refused:
            continue
        keep.append(m)
    return keep, fixes


def truncate_fake(msgs: list[dict]) -> tuple[list[dict], bool]:
    """End the sample at the call whose result isn't real (dry run, bridge down)."""
    for i, m in enumerate(msgs):
        body = str(m.get("content", "")) if m["role"] == "tool" else ""
        body = re.sub(r"^<tool_result[^>]*>\s*", "", body)
        if m["role"] == "tool" and (body.startswith(FAKE) or BRIDGE_DOWN in body):
            j = i
            while j > 0 and msgs[j - 1]["role"] == "tool":
                j -= 1
            return msgs[:j], True
    return msgs, False


def structured(msgs: list[dict]) -> list[dict]:
    """OpenAI-style messages with arguments as objects (LFM3's template refuses JSON strings)."""
    out = []
    for m in msgs:
        m = {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
        if m.get("tool_calls"):
            m["tool_calls"] = [{"id": c.get("id", ""), "type": "function",
                                "function": {"name": c["function"]["name"],
                                             "arguments": json.loads(c["function"].get("arguments") or "{}")
                                             if isinstance(c["function"].get("arguments"), str)
                                             else c["function"].get("arguments") or {}}}
                               for c in m["tool_calls"]]
        if m["role"] == "assistant" and not m.get("tool_calls"):
            m.pop("tool_calls", None)
        if m.get("content") is None:
            m["content"] = ""
        out.append(m)
    return out


# ---------------------------------------------------------------- LFM3's tool format, as text

def _value(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def render_calls(calls: list[dict]) -> str:
    parts = []
    for c in calls:
        f = c["function"]
        args = "".join(f"<parameter={k}>\n{_value(v)}\n</parameter>\n" for k, v in (f.get("arguments") or {}).items())
        parts.append(f"<tool_call>\n<function={f['name']}>\n{args}</function>\n</tool_call>")
    return "\n\n".join(parts)


def tools_block(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        fn = t["function"]
        lines.append("{" + ", ".join(f"{json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
                                     for k, v in fn.items()) + "}")
    return "<tools>\n" + "\n".join(lines) + "\n</tools>"


def as_text(msgs: list[dict], tools: list[dict]) -> list[dict]:
    """The same conversation with LFM3's tool syntax written into plain string contents."""
    out = []
    for m in msgs:
        if m["role"] == "system":
            out.append({"role": "system", "content": (m["content"] + "\n\n" if m["content"] else "") + tools_block(tools)})
        elif m["role"] == "assistant":
            text = m.get("content") or ""
            if "</think>" not in text:
                text = "<think></think>" + text
            out.append({"role": "assistant", "content": text + render_calls(m.get("tool_calls") or [])})
        elif m["role"] == "tool":
            body = m.get("content") or ""
            out.append({"role": "tool", "content": body if "<tool_response>" in body
                        else f"<tool_response>\n{body}\n</tool_response>"})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", help="traces JSON exported before (default: pull from the hub container)")
    ap.add_argument("--tools", help="tool schemas JSON exported before (default: pull from the hub container)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--list-unreviewed", action="store_true",
                    help="print the samples no eval case grades and review.json doesn't cover, then stop")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # traces hold non-ASCII text; a Windows console isn't UTF-8
    # turns no eval case grades, reviewed by hand: request_id -> {"keep": bool, "why": str}
    review_file = Path(__file__).with_name("review.json")
    review = json.loads(review_file.read_text(encoding="utf-8")) if review_file.exists() else {}
    if a.traces and a.tools:
        rows = json.loads(Path(a.traces).read_text(encoding="utf-8"))
        schemas = json.loads(Path(a.tools).read_text(encoding="utf-8"))
    else:
        rows, schemas = pull_from_hub()
    by_model = {t["name"]: t for t in schemas}
    fn_of = lambda name: {"type": "function", "function": {k: v for k, v in by_model[name].items()
                                                           if k not in ("internal", "risk")}}
    cases = case_index(json.loads((ROOT / "scripts" / "eval" / "cases.json").read_text(encoding="utf-8")))
    score = _run_eval_module().score

    stats, why_dropped, fixes_seen = Counter(), Counter(), Counter()
    samples, unreviewed = [], []
    for row in rows:
        loops = row["data"].get("loops") or []
        if row["route"] not in ("model", "plan") or not loops:
            stats["no model step (fast path, memory)"] += 1
            continue
        user_turns = [m for m in row["data"].get("messages") or [] if m.get("role") == "user"]
        said = (row.get("said") or (user_turns[-1]["content"] if user_turns else "")).strip()
        case = cases.get(said.lower())
        calls = turn_calls(row, by_model)
        good, why = grade(row, case, calls, score)
        if not good:
            why_dropped[why.split(":")[0]] += 1
            continue
        if case is None:  # "no errors" doesn't make the calls right: a person's review does
            r = review.get(row["request_id"])
            if r is None:
                unreviewed.append(row)
                why_dropped["not reviewed yet"] += 1
                continue
            if not r["keep"]:
                why_dropped["review: " + r["why"]] += 1
                continue
            why = "reviewed: " + r["why"]
        for k, loop in enumerate(loops):
            msgs, fixes = repair(loop["convo"])
            final = (loop.get("calls") or [{}])[-1]
            if k == len(loops) - 1 and final.get("content") and not final.get("tool_calls") \
                    and not BAD_REPLY.search(final["content"]):
                msgs = msgs + [{"role": "assistant", "content": final["content"]}]
            msgs, cut = truncate_fake(msgs)
            while msgs and msgs[-1]["role"] != "assistant":  # a sample ends on something to learn
                msgs.pop()
            if len(msgs) < 3 or msgs[0]["role"] != "system":
                why_dropped["nothing left to learn"] += 1
                continue
            called = [c["function"]["name"] for m in msgs for c in m.get("tool_calls") or []]
            if any(n not in by_model for n in called):
                why_dropped["calls a tool that no longer exists"] += 1
                continue
            stale = [c for m in msgs for c in m.get("tool_calls") or []
                     if set(json.loads(c["function"].get("arguments") or "{}") if isinstance(c["function"].get("arguments"), str)
                            else c["function"].get("arguments") or {})
                     - set(((by_model[c["function"]["name"]].get("parameters") or {}).get("properties") or {}))]
            if stale:
                why_dropped["uses arguments the tool no longer takes"] += 1
                continue
            offered = [n for n in loop.get("offered") or [] if n in by_model]
            tools = [fn_of(n) for n in dict.fromkeys(offered + called)]
            samples.append({"msgs": structured(msgs), "tools": tools, "fixes": fixes, "cut": cut,
                            "key": (said.lower(), tuple(called)), "rid": row["request_id"], "model": row["model"],
                            "dry": bool(row["data"].get("dry_run")), "case": case["id"] if case else "", "why": why})
            fixes_seen.update(f.split(":")[0] for f in fixes)

    if a.list_unreviewed:
        for row in unreviewed:
            steps = []
            for loop in row["data"]["loops"]:
                for m in loop["convo"]:
                    for c in m.get("tool_calls") or [] if m["role"] == "assistant" else []:
                        steps.append(f"{c['function']['name']}({(c['function'].get('arguments') or '')[:110]})")
            print(json.dumps({"id": row["request_id"], "said": (row.get("said") or "")[:220], "calls": steps,
                              "reply": (row.get("reply") or "")[:200]}, ensure_ascii=False))
        print(f"{len(unreviewed)} unreviewed", file=sys.stderr)
        return 0

    # near-duplicates: evals ran each case many times
    kept, per_key = [], Counter()
    for s in sorted(samples, key=lambda s: (s["dry"], s["cut"])):  # prefer real, complete conversations
        if per_key[s["key"]] < KEEP_PER_KEY:
            per_key[s["key"]] += 1
            kept.append(s)
    dupes = len(samples) - len(kept)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    files = {n: {"train": [], "val": []} for n in ("chatml", "openai")}
    for s in kept:
        split = "val" if int(hashlib.sha1(s["key"][0].encode()).hexdigest(), 16) % 10 == 0 else "train"
        files["chatml"][split].append({"messages": as_text(s["msgs"], s["tools"])})
        files["openai"][split].append({"messages": s["msgs"], "tools": s["tools"]})
    for name, parts in files.items():
        for split, recs in parts.items():
            path = out / f"lfm3_tools_{name}{'' if split == 'train' else '_val'}.jsonl"
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")

    by_model_n = Counter(s["model"] for s in kept)
    tool_n = Counter(c["function"]["name"] for s in kept for m in s["msgs"] for c in m.get("tool_calls") or [])
    no_tool = sum(1 for s in kept if not any(m.get("tool_calls") for m in s["msgs"]))
    report = [
        "# LFM3 tool-calling SFT set from JARVIS traces", "",
        f"From {len(rows)} traced turns: **{len(kept)} samples** "
        f"({len(files['chatml']['train'])} train, {len(files['chatml']['val'])} val).", "",
        "| | |", "|---|---|",
        f"| samples before de-duplication | {len(samples)} |",
        f"| near-duplicates dropped (same request and calls, over {KEEP_PER_KEY}) | {dupes} |",
        f"| samples with a repaired step | {sum(1 for s in kept if s['fixes'])} |",
        f"| samples ending at a tool call (dry run or bridge down) | {sum(1 for s in kept if s['cut'])} |",
        f"| samples answering without a tool | {no_tool} |",
        f"| graded by an eval case / reviewed by hand (review.json) | {sum(1 for s in kept if s['case'])} / "
        f"{sum(1 for s in kept if not s['case'])} |", "",
        "## Turns left out", "", *[f"- {k}: {v}" for k, v in (stats + why_dropped).most_common()], "",
        "## Repairs", "", *([f"- {k}: {v}" for k, v in fixes_seen.most_common()] or ["- none"]), "",
        "## Samples by the model that produced them", "", *[f"- {k}: {v}" for k, v in by_model_n.most_common()], "",
        "## Tool calls taught", "", *[f"- {k}: {v}" for k, v in tool_n.most_common()], "",
    ]
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report[:12]))


if __name__ == "__main__":
    sys.exit(main())
