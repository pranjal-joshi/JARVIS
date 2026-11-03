"""Tool selection only: for every eval case that expects a tool, is that tool among the ones offered to the
model, and how high does it rank? No model call, a few seconds for the whole suite, so tool cards and
descriptions (hub/src/jarvis_hub/tools/cards.py, the registration files) can be tuned against it: change a
card, rebuild the hub, run this again, keep the change if the numbers go up.

    python scripts/eval/rank_tools.py                  # all cases with an expected tool
    python scripts/eval/rank_tools.py --only hard      # categories or case ids
    python scripts/eval/rank_tools.py --cases my.json

A miss prints the tools that beat the expected one: add an example to its card that says what the request
says, or a "not for" line to the one that won. Standard library only."""

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

from run_eval import Hub, env

CORE = {"get_time", "calculate", "kb_search"}  # always offered: their rank says nothing


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma list of categories or case ids")
    ap.add_argument("--cases", help="case file (default: cases.json next to this script)")
    a = ap.parse_args()
    cases = json.loads(Path(a.cases or Path(__file__).with_name("cases.json")).read_text(encoding="utf-8"))
    if a.only:
        pick = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in pick or c.get("cat") in pick]
    e = env()
    hub = Hub({"web": e["HUB_TOKEN_WEB"]})
    rows, offered, top1, top3 = [], 0, 0, 0
    for c in cases:
        want = set(c.get("tools") or [])
        if not want:
            continue
        turns = c.get("turns") or [c["say"]]
        q = urllib.parse.urlencode({"say": turns[-1], "before": turns[-2] if len(turns) > 1 else ""})
        r = hub.json(f"/v1/tools/rank?{q}")
        ranked = [n for n in r["scores"] if n not in CORE or n in want]
        rank = min((ranked.index(n) + 1 for n in want if n in ranked), default=99)
        got = bool(want & set(r["offered"]))
        offered += got
        top1 += rank == 1
        top3 += rank <= 3
        if not got or rank > 1:
            beat = [f"{n} {r['scores'][n]:.2f}" for n in ranked[:min(rank - 1, 3)]]
            mine = max((r["scores"].get(n, 0) for n in want), default=0)
            rows.append(f"{'MISS' if not got else 'rank ' + str(rank):>7}  {c['id']:<24} {sorted(want)[0]} {mine:.2f}"
                        f"  beaten by: {', '.join(beat)}")
    n = sum(1 for c in cases if c.get("tools"))
    print(f"{n} cases: offered {offered}/{n}, ranked first {top1}/{n}, top 3 {top3}/{n}\n")
    print("\n".join(rows) or "every expected tool ranked first")
    sys.exit(0 if offered == n else 1)


if __name__ == "__main__":
    main()
