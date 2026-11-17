"""Background jobs: long work handed to Claude Code on the host by the expert bridge.

A job is a task in the user's words ("research X and write me a summary", "go through these logs and find the
errors"), optionally run through one of the user's Claude Code skills. The small local model only starts one
(start_job, approved in the HUD like any call that leaves the machine), asks how it is going (job_status) or stops
it (cancel_job). The work runs for minutes on the host, in a folder of its own with a dollar and a time cap
(bridge/expert_bridge.py). This module polls the bridge, shows progress in the HUD and, when a job ends, saves its
report as a notes page under jarvis/jobs/ (so "what did that job find?" is a plain notes search later) and tells
the user."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

from jarvis_hub.approval.gate import Risk
from jarvis_hub.automation.notify import Notifier
from jarvis_hub.config import Settings, Source
from jarvis_hub.events.bus import Event, EventBus
from jarvis_hub.kb.graph import ObsidianVault, frontmatter
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry
from jarvis_hub.files import write_atomic
from jarvis_hub.guard.refs import opaque
from jarvis_hub.guard.validate import actionable

log = logging.getLogger(__name__)
LIVE, ENDED = ("queued", "running"), ("done", "failed", "cancelled")
MAX_PAGE_REPORT = 20_000
# poll often while a job runs (the HUD shows its steps); otherwise often enough to notice a job started elsewhere
BUSY_S, IDLE_S = 5, 15
_FILLER = set("""a an the and or of to in on for about with from into by at is are be it this that these those my me i
you your please can could would will run start do make go find out tell show give get job background task as some any
then also just""".split())


def skill_name(said: str) -> str:
    """"/Some-Skill." or "some skill" as a small model writes it -> "some-skill"; "" for none."""
    s = re.sub(r"\s+", "-", (said or "").strip().lower())
    s = re.sub(r"[^a-z0-9_-]+", "", s).strip("-")
    return "" if s in ("none", "no", "null", "task", "general") else s


def label(j: dict) -> str:
    return j.get("skill") or "task"


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m} min" if m else f"{int(seconds)} s"


def describe(j: dict, now: float | None = None) -> str:
    now = now or time.time()
    head = f"{j['id']} · {label(j)} · {j['state']}"
    if j["state"] == "running":
        head += f" for {_ago(now - (j.get('started') or j['created']))}, {j.get('steps', 0)} steps; now: {j.get('last', '')}"
    elif j["state"] in ENDED:
        head += f" {_ago(now - (j.get('ended') or now))} ago"
        if j.get("cost_usd") is not None:
            head += f", ${j['cost_usd']}"
        if j.get("error"):
            head += f"; {j['error'][:160]}"
        elif j.get("summary"):
            head += f": {j['summary'][:300]}"
    return f"{head}\n  goal: {j['goal'][:200]}"


def page_name(j: dict) -> str:
    """Short: "jarvis/jobs/whoami triage b54d" (three words of the goal and a bit of the id), not the date, skill
    and full id: the name is repeated in every search hit and list."""
    goal = re.sub(r"\S*[\\/]\S*|\b[a-z]:\S*", " ", j.get("goal", "").lower())  # not the words of a path in it
    words = [w for w in re.findall(r"[a-z0-9]+", goal) if w not in _FILLER and len(w) > 1]
    return f"jarvis/jobs/{' '.join(words[:3]) or label(j)} {j['id'][:4]}"


def report_page(j: dict) -> tuple[str, str]:
    """The notes page for a finished job: summary first, then the report, one block per paragraph."""
    lines = [f"- {(j.get('summary') or j.get('error') or '').strip() or 'no summary'}",
             f"  goal:: {j['goal'][:300]}",
             *([f"  skill:: {j['skill']}"] if j.get("skill") else []),
             *([f"  target:: {j['target']}"] if j.get("target") else []),
             f"  ran:: {time.strftime('%Y-%m-%d', time.localtime(j.get('ended') or time.time()))}, "
             f"${j.get('cost_usd', '?')}, {j['state']}",
             f"  folder:: {j['dir']}",
             "- Report"]
    report = (j.get("report") or "").strip()
    if len(report) > MAX_PAGE_REPORT:
        report = report[:MAX_PAGE_REPORT] + f"\n\n(cut short; the full report is {j['dir']}\\REPORT.md)"
    for para in re.split(r"\n\s*\n", report) if report else ["(the job wrote no REPORT.md)"]:
        rows = [r.rstrip() for r in para.strip("\n").splitlines()]
        if rows:
            first = rows[0].lstrip()
            lines.append("\t- " + (first[2:] if first.startswith("- ") else rows[0]))
            lines += ["\t  " + r for r in rows[1:]]
    return page_name(j), "\n".join(lines)


PROJECT_GOAL = (
    "Build a small knowledge base about the project in the target folder, for the user's notes, read by a voice "
    "assistant. Read the README, docs, manifests, entry points and config first; don't read everything. Write 4 to 8 "
    "markdown pages into a folder named pages beside REPORT.md (your job folder, never the target: the target is "
    "read-only, and JARVIS copies the pages into the user's notes itself), one file each, named in lowercase words like "
    "overview.md, how-to-run.md, architecture.md, key-files.md, configuration.md, glossary.md; add or skip pages as the "
    "project needs. Each page: under 400 words; it starts with one or two plain sentences that answer what the page is "
    "about and can be read aloud; then short sections under ## headings, short bullets, exact commands in code blocks, "
    "file paths relative to the project. No tables, no badges, no secrets or tokens from config files. Link pages to "
    "each other with [[page name]] (the file name without .md). REPORT.md only lists the pages written. End with one "
    "sentence on what the project is and which pages you wrote.")


def project_pages(j: dict) -> list[tuple[str, str]]:
    """The KB pages a project job handed back (pages/*.md), as notes pages jarvis/projects/<folder>/<page>,
    with [[page]] links between them made full and frontmatter saying where they came from."""
    folder = re.split(r"[\\/]", (j.get("target") or "").rstrip("\\/"))[-1]
    project = " ".join(re.findall(r"[a-z0-9]+", folder.lower())) or j["id"][:4]
    names = {}
    for stem in j.get("pages") or {}:
        if short := " ".join(re.findall(r"[a-z0-9]+", stem.lower().replace("-", " "))):
            names[stem] = f"jarvis/projects/{project}/{short}"
    head = frontmatter({"project": project, "tags": "project", "source": j.get("target") or "",
                        "updated": time.strftime("%Y-%m-%d", time.localtime(j.get("ended") or time.time()))})

    def link(m: re.Match) -> str:
        key = next((k for k in names if k.lower() == m[1].strip().lower().replace(" ", "-")
                    or k.lower() == m[1].strip().lower()), None)
        return f"[[{names[key]}|{m[1].strip()}]]" if key else m[0]
    pages = {stem: re.sub(r"\[\[([^\]|]+)\]\]", link, text.strip()) for stem, text in (j.get("pages") or {}).items()
             if stem in names and text.strip()}
    # the overview is the project's hub (an Obsidian "map of content"): it links every other page
    if hub := next((s for s in pages if s.lower() == "overview"), None):
        missing = [s for s in pages if s != hub and f"[[{names[s]}|" not in pages[hub]]
        if missing:
            pages[hub] += "\n\n## Pages\n" + "".join(f"- [[{names[s]}|{s}]]\n" for s in missing)
    return [(names[s], head + pages[s].rstrip() + "\n") for s in pages]


class Jobs:
    def __init__(self, s: Settings, graph: ObsidianVault, notifier: Notifier | None, bus: EventBus,
                 seen_file: Path | None = None) -> None:
        self.s, self.graph, self.notifier, self.bus = s, graph, notifier, bus
        self.skills: list[str] = []  # skills a job may use, as the bridge reports them
        self.recent: list[dict] = []  # the bridge's latest list, for the HUD
        self.error = ""
        self._seen_file = seen_file
        self._reported: set[str] | None = None  # ended jobs already saved and announced
        self.dismissed: set[str] = set()  # ended jobs whose HUD card the user closed
        self.cleared: set[str] = set()  # ended jobs taken off the Monitors list too
        self._task: asyncio.Task | None = None
        self.on_change = None  # called after a report page is written (the hub re-indexes notes)
        try:
            if seen_file and seen_file.exists():
                saved = json.loads(seen_file.read_text(encoding="utf-8"))
                if isinstance(saved, list):  # the first format: reported ids only
                    saved = {"reported": saved, "dismissed": saved}
                self._reported = set(saved.get("reported") or [])
                self.dismissed = set(saved.get("dismissed") or [])
                self.cleared = set(saved.get("cleared") or [])
        except (OSError, ValueError, AttributeError):
            pass

    def cards(self) -> list[dict]:
        """The jobs for the HUD, each marked with `dismissed` (its card closed); cleared ones left out unless running."""
        return [{**j, "dismissed": j["id"] in self.dismissed} for j in self.recent
                if j["id"] not in self.cleared or j["state"] in LIVE]

    def clear(self) -> int:
        """Take every ended job off the Monitors list (and close its card). Returns how many."""
        ended = {j["id"] for j in self.recent if j["state"] in ENDED} - self.cleared
        self.cleared |= ended
        self.dismissed |= ended
        self._save_seen()
        return len(ended)

    def dismiss(self, jid: str) -> bool:
        """Close an ended job's card (a running job's card stays: stop it instead)."""
        j = next((j for j in self.recent if j["id"] == jid), None)
        if not j or j["state"] in LIVE:
            return False
        self.dismissed.add(jid)
        self._save_seen()
        return True

    async def _call(self, method: str, path: str, body: dict | None = None) -> tuple[bool, dict]:
        token = self.s.expert_bridge_token.get_secret_value()
        url = self.s.expert_bridge_url.rstrip("/") + path
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as c:
                r = await c.request(method, url, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            return False, {"detail": f"the expert bridge is not reachable ({type(e).__name__}). Is it running on the host?"}
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return r.status_code == 200, data if r.status_code == 200 else {"detail": data.get("detail") or r.status_code}

    async def start(self, skill: str, goal: str, target: str) -> tuple[bool, dict]:
        ok, data = await self._call("POST", "/v1/jobs", {"skill": skill, "goal": goal, "target": target})
        if ok:
            self._publish("set", data)
            await self.refresh()
        return ok, data

    async def cancel(self, jid: str) -> tuple[bool, dict]:
        ok, data = await self._call("POST", f"/v1/jobs/{jid}/cancel")
        if ok:
            await self.poll()
        return ok, data

    async def refresh(self) -> list[dict]:
        ok, data = await self._call("GET", "/v1/jobs")
        self.error = "" if ok else str(data.get("detail"))
        if ok:
            self.recent = data.get("jobs") or []
            self.skills = data.get("skills") or []
        return self.recent

    async def poll(self) -> list[dict]:
        """Refresh the list; save and announce every job that ended since the last look. Returns those."""
        before = {j["id"]: (j["state"], j.get("last"), json.dumps(j.get("todos"))) for j in self.recent}
        jobs = await self.refresh()
        if self.error:
            return []
        if self._reported is None:  # first ever look: what had already ended isn't news, nor a card to show
            self._reported = {j["id"] for j in jobs if j["state"] in ENDED}
            self.dismissed |= self._reported
            self._save_seen()
        finished = []
        for j in jobs:
            if j["state"] in LIVE and before.get(j["id"]) != (j["state"], j.get("last"), json.dumps(j.get("todos"))):
                self._publish("running", j)
            if j["state"] in ENDED and j["id"] not in self._reported:
                ok, full = await self._call("GET", f"/v1/jobs/{j['id']}")
                await self._finish(full if ok else j)
                finished.append(j)
        return finished

    async def _finish(self, j: dict) -> None:
        self._reported.add(j["id"])
        self._save_seen()
        page = ""
        if j["state"] != "cancelled":
            try:
                page, text = report_page(j)
                self.graph.write_jarvis_page(page, text, replace=True)
                kb = project_pages(j)
                for name, body in kb:
                    self.graph.write_jarvis_page(name, body, replace=True)
                if kb:  # what's said aloud: where the project pages are, not the report page
                    page = f"{len(kb)} pages under {kb[0][0].rsplit('/', 1)[0]}"
                if self.on_change:
                    self.on_change()
            except (OSError, ValueError) as e:
                log.warning("job %s report not saved: %s", j["id"], e)
                page = ""
        what = {"done": "finished", "failed": "failed", "cancelled": "was cancelled"}[j["state"]]
        text = (j.get("summary") or j.get("error") or "").strip()[:300]
        line = f"The background job {what}" + (f": {text}" if text else ".") + (f" Saved to notes: {page}." if page else "")
        if self.notifier:
            await self.notifier.send("Job " + what, line, ["hud", "windows"], origin="job")
        self._publish(j["state"], {**j, "page": page})
        log.info("job %s %s (page %s)", j["id"], j["state"], page or "-")

    def _publish(self, kind: str, j: dict) -> None:
        for src in (Source.WEB, Source.PHONE):
            self.bus.publish(Event("job_state", src, "", {"kind": kind, "id": j.get("id"), "skill": j.get("skill"),
                                                          "state": j.get("state"), "last": j.get("last", "")}))

    def _save_seen(self) -> None:
        if self._seen_file and self._reported is not None:
            try:
                write_atomic(self._seen_file, json.dumps({"reported": sorted(self._reported)[-500:],
                                                       "dismissed": sorted(self.dismissed)[-500:],
                                                       "cleared": sorted(self.cleared)[-500:]}), encoding="utf-8")
            except OSError as e:
                log.warning("job state not saved: %s", e)

    def start_polling(self) -> None:
        async def loop() -> None:
            while True:
                try:
                    await self.poll()
                except Exception:  # a failed look must not stop the next one
                    log.exception("job poll failed")
                busy = any(j["state"] in LIVE for j in self.recent)
                await asyncio.sleep(BUSY_S if busy else IDLE_S)
        self._task = asyncio.get_running_loop().create_task(loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


def register_job_tools(reg: ToolRegistry, jobs: Jobs) -> bool:
    token = jobs.s.expert_bridge_token.get_secret_value()
    if not token or token == "change-me":
        return False

    # job ids reach the model as this conversation's refs (guard/refs.py)
    opaque(["start_job", "job_status", "cancel_job"], "job", lambda: [j["id"] for j in jobs.recent])

    def _pick(jid: str) -> dict | None:
        jid = (jid or "").strip().lower()
        live = [j for j in jobs.recent if j["state"] in LIVE]
        if not jid or jid in ("it", "that", "the job", "job", "current", "running"):
            return live[0] if len(live) == 1 else (jobs.recent[0] if jobs.recent and not live else None)
        return next((j for j in jobs.recent if j["id"].startswith(jid) or jid in label(j) or jid in j["goal"].lower()),
                    None)

    async def start_job(args: dict, ctx: ToolCtx) -> str:
        goal = str(args.get("goal") or args.get("task") or "").strip()
        if not goal:
            return "ERROR: goal is missing. Call start_job again with goal: what the job should do, in the user's words."
        skill = skill_name(str(args.get("skill") or ""))
        if skill and not jobs.skills:
            await jobs.refresh()
        if skill and skill not in jobs.skills:
            skill = ""  # a guessed skill name: run it as a plain task rather than fail
        ok, j = await jobs.start(skill, goal, str(args.get("target") or "").strip())
        if not ok:
            return f"ERROR: the job didn't start: {j.get('detail')}. Tell the user why; don't retry."
        return (f"Started a background job ({j['id'][:4]}){f' with the {skill} skill' if skill else ''}. It runs on the "
                f"laptop for up to {j.get('timeout_s', 3600) // 60} minutes (at most ${j.get('budget_usd')}); you can "
                f"follow its steps on the HUD, and I'll tell you when it's done and save the result to your notes.")

    async def job_status(args: dict, ctx: ToolCtx) -> str:
        await jobs.refresh()
        if jobs.error:
            return actionable(f"ERROR: {jobs.error}")
        if not jobs.recent:
            return "No background jobs have run yet."
        j = _pick(str(args.get("job") or ""))
        return "\n".join(describe(x) for x in ([j] if j else jobs.recent[:5]))

    async def cancel_job(args: dict, ctx: ToolCtx) -> str:
        await jobs.refresh()
        j = _pick(str(args.get("job") or ""))
        if not j or j["state"] not in LIVE:
            live = [x["id"][:4] for x in jobs.recent if x["state"] in LIVE]
            return "No background job is running." if not live else f"ERROR: say which job. Running: {', '.join(live)}. Call cancel_job with one of those, or ask the user which."
        ok, data = await jobs.cancel(j["id"])
        return f"Cancelled the background job ({j['id'][:4]})." if ok else actionable(f"ERROR: {data.get('detail')}")

    reg.add(Tool(
        name="start_job", origin="expert",
        description=("Start a background job: long work (minutes) done on the laptop by Claude Code, a much stronger "
                     "agent that can search the web, read and write files and run commands. For research, analysis, "
                     "going through files or logs, writing a report. Use only when the user asks for a job or for "
                     "something to run in the background. The user approves it first. Reply with what this returns."),
        parameters={"type": "object", "properties": {
            "goal": {"type": "string", "description": "What the job should do or find out, in the user's words."},
            "target": {"type": "string", "description": "A file or folder path on the laptop to work on, only if the "
                                                        "user gave one."},
            "skill": {"type": "string", "description": "Only if the user names one of their Claude Code skills; "
                                                       "else leave empty."}},
            "required": ["goal"]},
        risk=Risk.EXTERNAL, call=start_job, final_answer=True))
    async def document_project(args: dict, ctx: ToolCtx) -> str:
        target = str(args.get("project") or args.get("path") or args.get("target") or "").strip().strip("\"'")
        if not target:
            return "ERROR: say which project: a folder path, or its folder name"
        ok, j = await jobs.start("", PROJECT_GOAL, target)
        if not ok:
            return f"ERROR: {j.get('detail')}"
        return (f"I'm having Claude read {j.get('target') or target} and write knowledge-base pages for it. It takes a "
                f"few minutes; I'll tell you when they're in your notes.")

    reg.add(Tool(
        name="document_project", origin="expert",
        description=("Have Claude Code read a project folder on the laptop and write knowledge-base pages about it "
                     "(overview, how to run, architecture, key files) into the notes. Runs as a background job; the "
                     "user approves it first. Reply with what this returns."),
        parameters={"type": "object", "properties": {
            "project": {"type": "string", "description": "The project's folder path, or just its folder name as "
                                                         "the user said it (e.g. 'jarvis')."}},
            "required": ["project"]},
        risk=Risk.EXTERNAL, call=document_project, final_answer=True))
    reg.add(Tool(
        name="job_status", origin="expert",
        description="How the background jobs are going, or what one found.",
        parameters={"type": "object", "properties": {
            "job": {"type": "string", "description": "The job's id or a word of its goal; empty for the latest."}}},
        risk=Risk.READ, call=job_status))
    reg.add(Tool(
        name="cancel_job", origin="expert",
        description="Stop a running background job.",
        parameters={"type": "object", "properties": {
            "job": {"type": "string", "description": "The job's id or a word of its goal; empty if only one runs."}}},
        risk=Risk.WRITE, call=cancel_job))
    return True
