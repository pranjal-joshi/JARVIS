"""Build an SFT dataset for LFM3 tool calling from JARVIS's own traces.

    python scripts/sft/build_sft.py                 # pulls traces + tool schemas from the running hub container
    python scripts/sft/build_sft.py --traces t.json --tools tools.json   # or from files exported before
    python scripts/sft/build_sft.py --traces old.db data/hub-local/traces.db   # trace databases (merged by request);
                                                    # without --tools, the schemas come from hub/.venv

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
    which tool, with what arguments);
  - from STRICT_SINCE on, tainted turns (outside text was read), skill runs and dry runs are left out;
  - a review verdict can carry a "fix": a turn that went wrong where the right call and reply are plain from the
    notes and tool outputs is kept corrected (see fixed());
  - remember no longer copies to the journal: "and today's journal" goes from old results, and a reply that
    still claims a journal write without one is left out.
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
BAD_REPLY = re.compile(r"(?i)couldn't quite catch|didn't catch that|\(dry run\)|would answer here|i ran into a problem|"
                       r"going in circles|haven't actually saved or changed anything|language model is not answering")
# the harness's guards refusing a wrong call: the refused step is dropped (a repair), the rest of the turn graded
GUARD = re.compile(r"you already called|didn't ask (?:about a time|you to write)|general knowledge, not current|"
                   r"which the user didn't say|would save words nobody said")
KEEP_PER_KEY = 2  # copies of the same request + tool calls (evals ran each case many times)
# turns from the second build on (5 Nov 2026): tainted turns, skill runs and dry runs are left out. The first build's
# turns were reviewed under the older rules, and its eval runs read notes and web pages in dry runs.
STRICT_SINCE = 1793817000  # 2026-11-05 00:00 IST
JOURNAL_COPY = re.compile(r"(\]\]) and today's journal\.")  # remember/correct_memory stopped writing one (13 Nov)
JOURNAL_CLAIM = re.compile(r"(?i)\b(?:logged|noted|saved|added|recorded)\b[^.]{0,60}\bjournal\b|journal (?:entry )?"
                           r"(?:has been|was) (?:added|recorded)")


def _run_eval_module():
    spec = importlib.util.spec_from_file_location("run_eval", ROOT / "scripts" / "eval" / "run_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_traces(paths: list[str]) -> list[dict]:
    """Traces from exported JSON or trace databases (the container's /data/traces.db, the host hub's
    data/hub-local/traces.db), one row per request."""
    rows = {}
    for path in paths:
        if path.endswith(".json"):
            got = json.loads(Path(path).read_text(encoding="utf-8"))
        else:
            got = json.loads(subprocess.run([sys.executable, "-", path], input=TRACES_PY.replace("'/data/traces.db'",
                             "sys.argv[1]").replace("import sqlite3", "import sqlite3, sys"), text=True,
                             capture_output=True, check=True, encoding="utf-8").stdout)
        for r in got:
            rows.setdefault(r["request_id"], r)
    return sorted(rows.values(), key=lambda r: r["ts"])


def local_tools() -> list[dict]:
    """Every tool's schema from the hub's own venv (the hub running on the host, not in a container)."""
    py = ROOT / "hub" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return json.loads(subprocess.run([str(py), "-"], input=TOOLS_PY, text=True, capture_output=True, check=True,
                                     encoding="utf-8", cwd=ROOT).stdout)  # the repo .env turns the device tools on


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
from jarvis_hub.persona.wit import Wit, register_wit_tool
from jarvis_hub.tools.widget import register_widget_tool
from jarvis_hub.tools.reminders import register_reminder_tools
from jarvis_hub.tools.utility import register_utility_tools
s, reg = Settings(), ToolRegistry()
for f in (lambda: register_kb_tools(reg, m(), m()), lambda: register_utility_tools(reg, m(), m()),
          lambda: register_expert_tool(reg, s), lambda: register_device_tools(reg, s),
          lambda: register_web_search(reg, s), lambda: register_notify_tool(reg, m()),
          lambda: register_reminder_tools(reg, m()), lambda: register_skill_tools(reg, m(), m()),
          lambda: register_dream_tool(reg, m()), lambda: register_job_tools(reg, Jobs(s, m(), None, EventBus())),
          lambda: register_widget_tool(reg), lambda: register_wit_tool(reg, Wit())):
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


def fixed(loop: dict, fix: dict, rid: str) -> list[dict]:
    """A corrected turn (a review.json "fix"): the loop's conversation up to the user's message (or its first `keep`
    messages; `user` replaces that message), then the right steps and reply. Without `steps`, the turn's own
    (repaired) steps stay and only the reply changes."""
    convo = loop["convo"]
    if "steps" not in fix:
        return repair(convo)[0] + [{"role": "assistant", "content": fix["reply"]}]
    n = fix.get("keep") or max(i for i, m in enumerate(convo) if m["role"] == "user") + 1
    msgs = [dict(m) for m in convo[:n]]
    if "user" in fix:
        msgs[-1]["content"] = fix["user"]
    for i, step in enumerate(fix["steps"]):
        cid = f"fix-{rid[:8]}-{i}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
            "name": step["call"], "arguments": json.dumps(step["args"], ensure_ascii=False)}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "name": step["call"],
                     "content": f'<tool_result tool="{step["call"]}">\n{step["result"]}\n</tool_result>'})
    return msgs + [{"role": "assistant", "content": fix["reply"]}]


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
    ap.add_argument("--traces", nargs="+", help="traces: JSON exported before or trace databases (default: pull "
                                                "from the hub container)")
    ap.add_argument("--tools", help="tool schemas JSON exported before (default: the container's, or with --traces "
                                    "the hub's own venv)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--list-unreviewed", action="store_true",
                    help="print the samples no eval case grades and review.json doesn't cover, then stop")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # traces hold non-ASCII text; a Windows console isn't UTF-8
    # turns no eval case grades, reviewed by hand: request_id -> {"keep": bool, "why": str, "fix": {...}}; a "no"
    # also overrules an eval pass
    review_file = Path(__file__).with_name("review.json")
    review = json.loads(review_file.read_text(encoding="utf-8")) if review_file.exists() else {}
    if a.traces:
        rows = load_traces(a.traces)
        schemas = json.loads(Path(a.tools).read_text(encoding="utf-8")) if a.tools else local_tools()
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
        r = review.get(row["request_id"])
        fix = (r or {}).get("fix")
        if r is not None and not r["keep"]:
            why_dropped["review: " + r["why"]] += 1
            continue
        if fix:
            why = "corrected: " + r["why"]
        else:
            new = row["ts"] >= STRICT_SINCE
            left_out = new and ("tainted (outside text read)" if row["data"].get("tainted") else
                                "skill run" if row["session"].startswith("skill-") else
                                "dry run" if row["data"].get("dry_run") else "")
            if left_out:
                why_dropped[left_out] += 1
                continue
            calls = turn_calls(row, by_model)
            good, why = grade(row, case, calls, score)
            if not good:
                why_dropped[why.split(":")[0]] += 1
                continue
            if case is None:  # "no errors" doesn't make the calls right: a person's review does
                if r is None:
                    unreviewed.append(row)
                    why_dropped["not reviewed yet"] += 1
                    continue
                why = "reviewed: " + r["why"]
        for k, loop in enumerate(loops):
            if fix and k != fix.get("loop", -1) % len(loops):  # a corrected turn: only the part put right
                continue
            if fix:
                msgs, fixes = fixed(loop, fix, row["request_id"]), []
            else:
                msgs, fixes = repair(loop["convo"])
                final = (loop.get("calls") or [{}])[-1]
                if k == len(loops) - 1 and final.get("content") and not final.get("tool_calls") \
                        and not BAD_REPLY.search(final["content"]):
                    msgs = msgs + [{"role": "assistant", "content": final["content"]}]
            msgs = [{**m, "content": JOURNAL_COPY.sub(r"\1.", m["content"])}
                    if m["role"] != "assistant" and isinstance(m.get("content"), str) else m for m in msgs]
            msgs, cut = truncate_fake(msgs)
            while msgs and msgs[-1]["role"] != "assistant":  # a sample ends on something to learn
                msgs.pop()
            if len(msgs) < 3 or msgs[0]["role"] != "system":
                why_dropped["nothing left to learn"] += 1
                continue
            called = [c["function"]["name"] for m in msgs for c in m.get("tool_calls") or []]
            if JOURNAL_CLAIM.search(msgs[-1].get("content") or "") and "add_to_journal" not in called:
                why_dropped["reply claims a journal write"] += 1
                continue
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
            # corrected turns are each their own example ("save that" means a different thing every time)
            key = (said.lower() + (" " + row["request_id"] if fix else ""), tuple(called))
            samples.append({"msgs": structured(msgs), "tools": tools, "fixes": fixes, "cut": cut,
                            "key": key, "rid": row["request_id"], "model": row["model"],
                            "dry": bool(row["data"].get("dry_run")), "case": case["id"] if case else "", "why": why,
                            "corrected": bool(fix), "new": row["ts"] >= STRICT_SINCE})
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
    for s in sorted(samples, key=lambda s: (not s["corrected"], s["dry"], s["cut"])):  # corrected, then real, complete
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
        f"| corrected turns (review.json fixes) | {sum(1 for s in kept if s['corrected'])} |",
        f"| samples from turns since 5 Nov 2026 (of them corrected) | {sum(1 for s in kept if s['new'])} "
        f"({sum(1 for s in kept if s['new'] and s['corrected'])}) |",
        f"| samples ending at a tool call (dry run or bridge down) | {sum(1 for s in kept if s['cut'])} |",
        f"| samples answering without a tool | {no_tool} |",
        f"| graded by an eval case / reviewed by hand (review.json) | {sum(1 for s in kept if s['case'])} / "
        f"{sum(1 for s in kept if not s['case'])} |", "",
        "## Turns left out", "", *[f"- {k}: {v}" for k, v in (stats + why_dropped).most_common()], "",
        "## Repairs", "", *([f"- {k}: {v}" for k, v in fixes_seen.most_common()] or ["- none"]), "",
        "## Samples by the model that produced them", "", *[f"- {k}: {v}" for k, v in by_model_n.most_common()], "",
        "## Tool calls taught", "", *[f"- {k}: {v}" for k, v in tool_n.most_common()], "",
    ]
    old = (out / "report.md").read_text(encoding="utf-8") if (out / "report.md").exists() else ""
    notes = old[old.index("## Notes"):] if "## Notes" in old else ""  # written by hand: kept across rebuilds
    (out / "report.md").write_text("\n".join(report) + ("\n" + notes if notes else ""), encoding="utf-8")
    print("\n".join(report[:12]))


if __name__ == "__main__":
    sys.exit(main())
