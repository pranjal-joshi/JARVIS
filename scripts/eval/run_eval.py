"""Eval suite for the hub: fixed prompts, scored on tool choice, arguments, reply content, style and
latency. Every request carries X-Jarvis-Dry-Run, so nothing is saved, sent, searched or switched:
tools that change anything or send data out are recorded in the audit and not run. Read-only tools
(notes, weather, time, laptop and phone status) do run.

    python scripts/eval/run_eval.py                    # all cases, current model
    python scripts/eval/run_eval.py --only memory,web  # categories or ids
    python scripts/eval/run_eval.py --model jarvis-2b-gpu   (switches the hub, then switches back)
    python scripts/eval/run_eval.py --cases my_cases.json   (another case file, same format)

Each failure also gets a kind, counted in the report: skipped (a tool was expected, none ran), wrong tool,
rejected (the call was refused before running: bad arguments, an unknown tool, a hook), tool error, false
claim (the reply says something was done and no tool did it), reply, style.

Writes docs/testing/evals/<date>-<model>.md and .json, and posts the summary to the hub (HUD → Evals).
Standard library only."""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HUB = "http://127.0.0.1:8700"  # not localhost: on Windows that tries IPv6 first and waits 2 s
MAX_SENTENCES = {"web": 4, "satellite": 2}
STYLE = [("bullets", re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s")), ("bold", re.compile(r"\*\*|__")),
         ("offer", re.compile(r"(?i)(would you like|let me know|anything else|feel free|shall i (also|look))")),
         ("emoji", re.compile("[\U0001F300-\U0001FAFF☀-➿]"))]
CLAIM = re.compile(r"(?i)^\W*(noted|saved|done|sent)\b|\bI(?:'ve| have|'ll| will)\s+(?:set|sent|saved|noted|added|"
                   r"turned|opened|scheduled|pinged|notified|texted|messaged|called)\b")


def env() -> dict:
    lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    return {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in lines if "=" in l and not l.startswith("#"))}


class Hub:
    def __init__(self, tokens: dict) -> None:
        self.tokens = tokens

    def call(self, path: str, body=None, source="web", headers=None, timeout=300):
        h = {"Authorization": f"Bearer {self.tokens[source]}", "Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(HUB + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=h, method="POST" if body is not None else "GET")
        return urllib.request.urlopen(req, timeout=timeout)

    def json(self, path: str, body=None, source="web"):
        with self.call(path, body, source) as r:
            return json.load(r)

    def ask(self, case: dict) -> dict:
        """Stream one turn; return reply, request id, time to first words and total time."""
        source = case.get("source", "web")
        msgs = [{"role": "user", "content": t} for t in (case["turns"] if "turns" in case else [case["say"]])]
        t0 = time.time()
        first, reply, rid, streamed = None, "", None, ""
        with self.call("/v1/chat/completions", {"model": "jarvis", "stream": True, "messages": msgs}, source,
                       {"X-Jarvis-Dry-Run": "1", "X-Satellite-Id": "eval-" + case["id"]}) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                ev = json.loads(line[5:])
                j = ev.get("jarvis") or {}
                if j.get("reset"):
                    streamed = ""
                piece = ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if piece:
                    first = first or time.time() - t0
                    streamed += piece
                if "final" in j:
                    reply, rid = j["final"], j.get("request_id")
        return {"reply": reply or streamed, "request_id": rid, "first_s": round(first or time.time() - t0, 1),
                "total_s": round(time.time() - t0, 1)}


def sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if s.strip()])


def days_until(month_day: str) -> int:
    """Days from today to the next month-day ("12-25"), for cases whose answer moves with the date."""
    today = date.today()
    m, d = map(int, month_day.split("-"))
    target = date(today.year, m, d)
    return ((target if target >= today else date(today.year + 1, m, d)) - today).days


def score(case: dict, out: dict, calls: list[dict]) -> list[str]:
    """Failures for one case ([] = pass). Each starts with its kind: "skipped: ...", "style: ..." etc."""
    fails, ran = [], [c for c in calls if c.get("status") != "rejected"]
    names = [c["tool"] for c in ran]
    for c in calls:
        if c.get("status") == "rejected":
            fails.append(f"rejected: {c['tool']}: {(c.get('result') or '')[:90]}")
        elif c.get("status") == "error":
            fails.append(f"tool error: {c['tool']}: {(c.get('result') or '')[:90]}")
    if "tools" in case:
        if case["tools"] == [] and names:
            fails.append(f"wrong tool: called {names}, expected no tool")
        elif case["tools"] and not names:
            fails.append(f"skipped: called nothing, expected one of {case['tools']}")
        elif case["tools"] and not set(names) & set(case["tools"]):
            fails.append(f"wrong tool: called {names}, expected one of {case['tools']}")
    acted = any(c.get("risk") != "read" or "screenshot" in c["tool"] for c in ran)  # a screenshot saves a file
    if CLAIM.search(out["reply"]) and not acted and case.get("tools") != []:
        fails.append("false claim: the reply says it was done, no tool did it")
    if bad := set(names) & set(case.get("not", [])):
        fails.append(f"wrong tool: called forbidden {sorted(bad)}")
    if case.get("args") and case.get("tools"):
        call = next((c for c in calls if c["tool"] in case["tools"]), None)
        if call:
            args = json.loads(call["arguments"] or "{}")
            for k, want in case["args"].items():
                if want.lower() not in str(args.get(k, "")).lower():
                    fails.append(f"args: {call['tool']}.{k} = {args.get(k)!r}, expected {want!r}")
    reply = out["reply"]
    want = case.get("reply") or (rf"\b{days_until(case['days_until'])}\b" if case.get("days_until") else "")
    if want and not re.search(want, reply):
        fails.append(f"reply: lacks /{want}/")
    if case.get("no_reply") and re.search(case["no_reply"], reply):
        fails.append(f"reply: matches /{case['no_reply']}/")
    for name, rx in STYLE:
        if rx.search(reply) and "```" not in reply:
            fails.append(f"style: {name}")
    cap = MAX_SENTENCES[case.get("source", "web")]
    if sentences(reply) > cap and "```" not in reply:
        fails.append(f"style: {sentences(reply)} sentences > {cap}")
    return fails


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma list of categories or case ids")
    ap.add_argument("--model", help="run against this model (the hub is switched back afterwards)")
    ap.add_argument("--no-post", action="store_true", help="don't send the summary to the hub")
    ap.add_argument("--cases", help="case file (default: cases.json next to this script)")
    a = ap.parse_args()
    e = env()
    hub = Hub({"web": e["HUB_TOKEN_WEB"], "satellite": e["HUB_TOKEN_SATELLITE"]})
    cases = json.loads(Path(a.cases or Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["cat"] in keep or c["id"] in keep]
    before = hub.json("/v1/llm")["model"]
    model = a.model or before
    if model != before:
        hub.json("/v1/llm", {"model": model})
    rows = []
    try:
        for case in cases:
            try:
                out = hub.ask(case)
                calls = [r for r in hub.json("/v1/audit?limit=60") if r["request_id"] == out["request_id"]][::-1]
                fails = score(case, out, calls)
            except Exception as ex:  # a hub error is a failed case, not a crashed run
                out, calls, fails = {"reply": "", "first_s": 0, "total_s": 0}, [], [f"hub error: {ex}"]
            hard = [f for f in fails if not f.startswith("style")]
            rows.append({"id": case["id"], "cat": case["cat"], "say": case.get("say") or " / ".join(case.get("turns", [])),
                         "pass": not fails, "tool_pass": not hard,
                         "fails": fails, "tools": [c["tool"] for c in calls if c.get("status") != "rejected"],
                         # phone and laptop replies can quote messages and notifications: not kept
                         "reply": "" if case["cat"] == "devices" else out["reply"][:300],
                         "first_s": out["first_s"], "total_s": out["total_s"]})
            print(f"{'PASS' if not fails else 'FAIL'} {out['first_s']:5.1f}s {out['total_s']:5.1f}s {case['id']:18} "
                  f"{[c['tool'] for c in calls]} {'; '.join(fails)}", flush=True)
    finally:
        if model != before:
            hub.json("/v1/llm", {"model": before})
    report(model, rows, post=not a.no_post, hub=hub)


def report(model: str, rows: list[dict], post: bool, hub: Hub) -> None:
    n, ok = len(rows), sum(r["pass"] for r in rows)
    cats: dict[str, list] = {}
    for r in rows:
        cats.setdefault(r["cat"], []).append(r)
    lat = sorted(r["first_s"] for r in rows if r["first_s"])
    kinds: dict[str, int] = {}
    for r in rows:
        for f in r["fails"]:
            k = f.split(":", 1)[0]
            kinds[k] = kinds.get(k, 0) + 1
    summary = {"model": model, "when": datetime.now().isoformat(timespec="minutes"), "cases": n, "passed": ok,
               "passed_ignoring_style": sum(r["tool_pass"] for r in rows), "failure_kinds": kinds,
               "first_words_median_s": lat[len(lat) // 2] if lat else None,
               "by_category": {c: f"{sum(r['pass'] for r in rs)}/{len(rs)}" for c, rs in cats.items()},
               "rows": rows}
    out = ROOT / "docs" / "testing" / "evals"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y-%m-%d-%H%M}-{model.replace(':', '_')}"
    (out / f"{stem}.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    md = [f"# Eval: {model}, {summary['when']}", "",
          f"**{ok}/{n} passed** ({summary['passed_ignoring_style']} ignoring style), median time to first words "
          f"{summary['first_words_median_s']} s.", "",
          "Failure kinds: " + (", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])) or "none"), "",
          "| Category | Passed |", "|---|---|", *[f"| {c} | {v} |" for c, v in summary["by_category"].items()], "",
          "| Case | Result | First words | Total | Tools | Notes |", "|---|---|---|---|---|---|"]
    md += [f"| {r['id']} | {'pass' if r['pass'] else 'FAIL'} | {r['first_s']} s | {r['total_s']} s | "
           f"{', '.join(r['tools']) or '-'} | {'; '.join(r['fails']).replace('|', '/')} |" for r in rows]
    (out / f"{stem}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n{ok}/{n} passed ({summary['passed_ignoring_style']} ignoring style); failure kinds {kinds}; "
          f"median first words {summary['first_words_median_s']} s -> {out / stem}.md")
    if post:
        try:
            hub.json("/v1/evals", summary)
        except Exception as ex:
            print(f"(not posted to the hub: {ex})", file=sys.stderr)


if __name__ == "__main__":
    main()
