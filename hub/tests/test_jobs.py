import asyncio
import json
import re

import httpx

from jarvis_hub.agent.tool_select import NAMED
from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Settings
from jarvis_hub.events.bus import EventBus
from jarvis_hub.expert import jobs as jobs_mod
from jarvis_hub.expert.jobs import Jobs, page_name, register_job_tools, report_page, skill_name
from jarvis_hub.kb.graph import LogseqGraph
from jarvis_hub.mcp.registry import ToolRegistry


class FakeBridge:
    """The bridge's /v1/jobs endpoints, in memory."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.posts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/jobs":
            body = json.loads(request.read())
            self.posts.append(body)
            j = {"id": "abcd1234", "skill": body["skill"], "goal": body["goal"], "target": body["target"],
                 "state": "running", "dir": "C:\\jobs\\x", "created": 1.0, "started": 1.0, "steps": 0,
                 "last": "starting…", "budget_usd": 5.0, "timeout_s": 3600}
            self.jobs[j["id"]] = j
            return httpx.Response(200, json=j)
        if m := re.fullmatch(r"/v1/jobs/(\w+)/cancel", path):
            self.jobs[m[1]].update(state="cancelled", ended=2.0)
            return httpx.Response(200, json=self.jobs[m[1]])
        if m := re.fullmatch(r"/v1/jobs/(\w+)", path):
            return httpx.Response(200, json={**self.jobs[m[1]], "report": "# Findings\n\nIt stores settings in INI.\n\n- key a\n- key b"})
        if path == "/v1/jobs":
            return httpx.Response(200, json={"jobs": list(self.jobs.values()), "skills": ["laya-train-eval"]})
        return httpx.Response(404, json={"detail": "not found"})


def _setup(monkeypatch, tmp_path):
    bridge = FakeBridge()
    real = httpx.AsyncClient
    monkeypatch.setattr(jobs_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(bridge), **kw))
    (tmp_path / "pages").mkdir()
    (tmp_path / "journals").mkdir()
    graph = LogseqGraph(str(tmp_path), "test")
    bus = EventBus()
    sent = []

    class Notifier:
        async def send(self, title, text, targets, url="", origin=""):
            sent.append((title, text, targets))

    jobs = Jobs(Settings(expert_bridge_token="t0k"), graph, Notifier(), bus, tmp_path / "seen.json")
    reg = ToolRegistry()
    assert register_job_tools(reg, jobs)
    return bridge, jobs, reg, graph, sent


def test_off_without_bridge_token(tmp_path):
    jobs = Jobs(Settings(expert_bridge_token=""), None, None, EventBus())
    assert not register_job_tools(ToolRegistry(), jobs)


def test_skill_names_are_cleaned_and_optional():
    assert skill_name("/Laya-Train-Eval.") == "laya-train-eval"
    assert skill_name("laya train eval") == "laya-train-eval"
    assert skill_name("") == "" and skill_name("none") == ""


def test_page_names_are_short():
    j = {"id": "b54d7f86", "goal": "Quick triage only: say what this executable whoami is", "skill": ""}
    assert page_name(j) == "jarvis/jobs/quick triage only b54d"
    assert page_name({"id": "abcd1234", "goal": "?!", "skill": ""}) == "jarvis/jobs/task abcd"


def test_start_needs_approval_and_a_finished_job_lands_in_notes(monkeypatch, tmp_path):
    bridge, jobs, reg, graph, sent = _setup(monkeypatch, tmp_path)
    start = reg.get("start_job")
    assert start.risk is Risk.EXTERNAL and start.final_answer
    out = asyncio.run(start.call({"goal": "how does foo save settings", "target": ""}, None))
    assert "abcd" in out and bridge.posts[0]["skill"] == ""
    assert "ERROR" in asyncio.run(start.call({"skill": "x"}, None))  # no goal
    asyncio.run(start.call({"skill": "Laya-Train-Eval.", "goal": "retrain"}, None))
    assert bridge.posts[-1]["skill"] == "laya-train-eval"
    asyncio.run(start.call({"skill": "made-up", "goal": "retrain"}, None))
    assert bridge.posts[-1]["skill"] == ""  # a guessed skill runs as a plain task

    asyncio.run(jobs.poll())  # first look: still running, nothing to report
    assert "running" in asyncio.run(reg.get("job_status").call({}, None))
    bridge.jobs["abcd1234"].update(state="done", ended=3.0, summary="Settings live in an INI file.", cost_usd=0.42)
    finished = asyncio.run(jobs.poll())
    assert [j["id"] for j in finished] == ["abcd1234"]
    page = graph.page("jarvis/jobs/retrain abcd")  # the fake bridge keeps one job id: the last goal was "retrain"
    text = page.path.read_text(encoding="utf-8")
    assert "Settings live in an INI file." in text and "It stores settings in INI." in text and "$0.42" in text
    assert sent and sent[0][0] == "Job finished"
    assert asyncio.run(jobs.poll()) == []  # announced once only


def test_cancel_picks_the_running_job(monkeypatch, tmp_path):
    bridge, jobs, reg, graph, sent = _setup(monkeypatch, tmp_path)
    asyncio.run(reg.get("start_job").call({"goal": "decompile it"}, None))
    out = asyncio.run(reg.get("cancel_job").call({}, None))
    assert out.startswith("Cancelled") and bridge.jobs["abcd1234"]["state"] == "cancelled"
    assert not any(p.name.lower().startswith("jarvis/jobs/") for p in graph.pages())  # nothing to save


def test_report_page_is_logseq_blocks():
    name, text = report_page({"id": "x1", "skill": "s", "state": "done", "goal": "g", "dir": "d", "ended": 0,
                              "summary": "ok", "report": "a\nb\n\n- c"})
    assert name == "jarvis/jobs/s x1" and text.startswith("- ok\n  goal:: g\n  skill:: s")
    assert "\t- a\n\t  b" in text and "\t- c" in text


def test_job_tools_are_offered_only_when_asked_for_a_job():
    for said in ("run a job to print hello", "run it as a job - web search for x", "background job",
                 "do this in the background: compare x", "launch a long-running task"):
        assert NAMED["start_job"].search(said), said
    for said in ("what's my job title", "good job jarvis", "read my notes", "run the hello world skill"):
        assert not NAMED["start_job"].search(said), said
    assert NAMED["job_status"].search("how's the job going") and NAMED["cancel_job"].search("cancel that job")


def test_a_finished_card_stays_until_dismissed(monkeypatch, tmp_path):
    bridge, jobs, reg, graph, sent = _setup(monkeypatch, tmp_path)
    asyncio.run(jobs.poll())  # first look (nothing yet)
    asyncio.run(reg.get("start_job").call({"goal": "tidy the logs"}, None))
    assert not jobs.dismiss("abcd1234")  # a running card can't be dismissed, only stopped
    bridge.jobs["abcd1234"].update(state="done", ended=3.0, summary="ok")
    asyncio.run(jobs.poll())
    assert [c["dismissed"] for c in jobs.cards()] == [False]  # ended, still shown
    assert jobs.dismiss("abcd1234") and jobs.cards()[0]["dismissed"]
    again = Jobs(jobs.s, graph, None, EventBus(), tmp_path / "seen.json")  # survives a restart
    assert "abcd1234" in again.dismissed and "abcd1234" in again._reported


def test_old_seen_file_format_still_loads(tmp_path):
    (tmp_path / "seen.json").write_text('["old1"]', encoding="utf-8")
    j = Jobs(Settings(expert_bridge_token="t"), None, None, EventBus(), tmp_path / "seen.json")
    assert j._reported == {"old1"} and j.dismissed == {"old1"}


def test_clearing_takes_finished_jobs_off_the_list(monkeypatch, tmp_path):
    bridge, jobs, reg, graph, sent = _setup(monkeypatch, tmp_path)
    asyncio.run(jobs.poll())
    asyncio.run(reg.get("start_job").call({"goal": "tidy the logs"}, None))
    assert jobs.clear() == 0 and len(jobs.cards()) == 1  # a running job stays
    bridge.jobs["abcd1234"].update(state="done", ended=3.0, summary="ok")
    asyncio.run(jobs.poll())
    assert jobs.clear() == 1 and jobs.cards() == [] and "abcd1234" in jobs.dismissed
    again = Jobs(jobs.s, graph, None, EventBus(), tmp_path / "seen.json")
    assert "abcd1234" in again.cleared
