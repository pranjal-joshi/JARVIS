"""Flag the turns in recent conversations that likely went wrong, from the traces the hub keeps (audit/traces.py):
the input for improving the harness, by hand or by a cloud session each night.

    python scripts/eval/review_traces.py                 # the last 24 hours
    python scripts/eval/review_traces.py --hours 1
    python scripts/eval/review_traces.py --base http://localhost:8700 --token $HUB_TOKEN_WEB

Flags: claim (the reply says something was done and no tool of that kind ran), not done (the harness said so
instead), circles, empty (no answer put together), no model (the model failed), rate limited, fallback,
tool error (a call refused or failed), corrected (the user's next words push back: "is it really set?",
"I asked...", "why didn't..."), slow. Each flagged turn is listed with what was said, the tools that ran and
the reply. Writes docs/testing/traces/<date>.md (and .json with --json). Standard library only."""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLOW_S = 20
# the reply says it did something (a rough copy of agent/claims.py; the trace says which tools really ran)
CLAIM = re.compile(r"^\W*(?:noted|saved|done|sent|added|logged|recorded|set)\b|\bI(?:'ve| have)\s+(?:just\s+)?"
                   r"(?:saved|noted|stored|added|logged|recorded|sent|set|scheduled|started|created|moved|turned|"
                   r"paused|locked|opened)\b|\bthat'?s done\b|\blet me (?:save|set|send|note|add)\b", re.I)
KIND = (("timer", re.compile(r"\b(?:timer|reminder|alarm)s?\b", re.I), {"set_timer", "set_reminder", "cancel_timer",
                                                                         "cancel_reminder"}),
        ("notes", re.compile(r"\b(?:saved|noted|stored|remember|journal|notes?|memory|logged|recorded)\b", re.I),
         {"kb_remember", "kb_journal", "kb_append_to_page", "kb_correct", "kb_move_item"}),
        ("message", re.compile(r"\b(?:sent|texted|messaged|called|notif)", re.I),
         {"adb_sms_send", "adb_whatsapp_send", "adb_call", "notify"}))
REPLIES = (("not done", re.compile(r"\bhaven't actually\b", re.I)),
           ("circles", re.compile(r"going in circles", re.I)),
           ("empty", re.compile(r"didn't catch that|couldn't put an answer together", re.I)),
           ("no model", re.compile(r"language model is not answering", re.I)),
           ("rate limited", re.compile(r"\bis rate limited\b", re.I)))
PUSHBACK = re.compile(r"^\W*(?:is it (?:really|actually)|did you (?:really|actually)?|why (?:didn'?t|did|it didn'?t|"
                      r"is)|I (?:asked|said|told you)|no\b|nope|that'?s (?:wrong|not)|you didn'?t|can'?t see|cant see|"
                      r"not what I|wrong|damn|come on|you should)", re.I)


def fetch(base: str, path: str, token: str):
    req = urllib.request.Request(base.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ran(turn: dict) -> list[str]:
    """The tools that ran (or were allowed to) this turn, in order."""
    return [x["tool"] for x in (turn.get("data") or {}).get("trace", [])
            if x.get("tool") and x.get("decision") in ("allow", "dry-run", "grant", "picked", "fixed", "ask")]


def tool_errors(turn: dict) -> list[str]:
    out = []
    for loop in (turn.get("data") or {}).get("loops", []):
        for m in loop.get("convo", []):
            body = str(m.get("content") or "") if m.get("role") == "tool" else ""
            if "ERROR:" in body[:200]:
                out.append(body.split("ERROR:", 1)[1].strip().split("\n")[0][:120])
    return list(dict.fromkeys(out))


def flags(turn: dict, nxt: dict | None) -> list[str]:
    reply, tools = turn.get("reply") or "", set(ran(turn))
    out = [name for name, rx in REPLIES if rx.search(reply)]
    if CLAIM.search(reply) and "not done" not in out:
        kind = next(((k, names) for k, rx, names in KIND if rx.search(reply)), None)
        if kind and not tools & kind[1] or not kind and not tools:
            out.append(f"claim ({kind[0] if kind else 'action'})")
    if any(x.get("decision") == "fallback" for x in (turn.get("data") or {}).get("trace", [])):
        out.append("fallback")
    if errs := tool_errors(turn):
        out.append("tool error: " + "; ".join(errs[:2]))
    if nxt and PUSHBACK.search(nxt.get("said") or ""):
        out.append(f"corrected: \"{(nxt.get('said') or '')[:80]}\"")
    if (turn.get("seconds") or 0) > SLOW_S:
        out.append(f"slow ({turn['seconds']:.0f} s)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--base", default=os.environ.get("JARVIS_HUB", "http://localhost:3001/api/hub"),
                    help="the HUD's hub proxy (default) or the hub itself with --token")
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN_WEB", ""))
    ap.add_argument("--json", action="store_true", help="also write the flagged turns as JSON")
    ap.add_argument("--out", default=str(ROOT / "docs" / "testing" / "traces"))
    a = ap.parse_args()
    since = time.time() - a.hours * 3600
    try:
        sessions = [s for s in fetch(a.base, "/traces", a.token) if s.get("last", 0) >= since]
    except OSError as e:
        sys.exit(f"can't reach the traces at {a.base}: {e}")
    rows, total = [], 0
    for s in sessions:
        got = fetch(a.base, f"/traces/{s['session']}", a.token)
        turns = sorted(got if isinstance(got, list) else got.get("turns", []), key=lambda t: t["ts"])
        for i, t in enumerate(turns):
            if t["ts"] < since:
                continue
            total += 1
            if f := flags(t, turns[i + 1] if i + 1 < len(turns) else None):
                rows.append({"session": s["session"], "ts": t["ts"], "model": t.get("model"), "route": t.get("route"),
                             "seconds": t.get("seconds"), "said": t.get("said"), "reply": t.get("reply"),
                             "tools": ran(t), "flags": f})
    counts: dict[str, int] = {}
    for r in rows:
        for f in r["flags"]:
            k = f.split(" (")[0].split(":")[0]
            counts[k] = counts.get(k, 0) + 1
    stamp = datetime.now()
    lines = [f"# Trace review {stamp:%Y-%m-%d %H:%M}", "",
             f"Last {a.hours:g} h: {len(sessions)} conversations, {total} turns, {len(rows)} flagged.", "",
             "Flags: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])) or "none"), ""]
    for r in rows:
        lines += [f"## {datetime.fromtimestamp(r['ts']):%H:%M:%S} · {r['session']} · {r['model']} · {r['route']} · "
                  f"{r['seconds'] or 0:.1f} s", "",
                  f"- **Flags:** {'; '.join(r['flags'])}",
                  f"- **Said:** {r['said'][:400]}",
                  f"- **Tools:** {', '.join(r['tools']) or 'none'}",
                  f"- **Reply:** {(r['reply'] or '')[:500]}", ""]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    md = out / f"{stamp:%Y-%m-%d-%H%M}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    if a.json:
        md.with_suffix(".json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n".join(lines[:5]))
    print(f"report: {md}")


if __name__ == "__main__":
    main()
