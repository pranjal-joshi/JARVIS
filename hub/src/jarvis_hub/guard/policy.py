"""Policy: what each tool call may do without a human, as rules in a file you can read and edit.

Replaces the flat APPROVAL_RISKS list. Order of decision for one call:
  1. a grant ("allow for 1 hour", given on an approval card)   -> allow
  2. the first rule that matches (tool, source, arguments, hours) -> its action
  3. the default for the tool's risk level                       -> allow / ask / deny / dry-run
then budgets (external calls per day, writes per hour) can turn an allow into a deny.

The file is part of the repo (policy/policy.yaml, mounted read-only into the hub), never the
knowledge base: KB pages can be written by JARVIS and by what it reads, the policy can't."""

import fnmatch
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Source

log = logging.getLogger(__name__)
ACTIONS = ("allow", "ask", "deny", "dry-run")


@dataclass
class Rule:
    tool: str = "*"  # glob over internal or model-facing name
    action: str = "ask"
    source: list[str] = field(default_factory=list)  # empty = any
    args: dict[str, str] = field(default_factory=dict)  # argument -> regex it must match
    hours: str = ""  # "22-07" local time; empty = any time
    why: str = ""

    def matches(self, tool, args: dict, source: Source, now: datetime) -> bool:
        if not (fnmatch.fnmatch(tool.name, self.tool) or fnmatch.fnmatch(tool.model_name, self.tool)):
            return False
        if self.source and source.value not in self.source:
            return False
        if self.hours:
            start, end = (int(x) for x in self.hours.split("-"))
            inside = start <= now.hour < end if start < end else now.hour >= start or now.hour < end
            if not inside:
                return False
        return all(re.search(rx, str(args.get(k, "")), re.I) for k, rx in self.args.items())


@dataclass
class Decision:
    action: str  # allow | ask | deny | dry-run
    why: str = ""
    rule: str = ""  # which rule or default decided, for the audit and the "why" panel


class Policy:
    def __init__(self, path: str | None, ask_for: frozenset, db_path: str) -> None:
        self.path = Path(path) if path else None
        self.ask_for = ask_for  # APPROVAL_RISKS: the defaults when there is no policy file
        self.defaults = {r.value: ("ask" if r in ask_for else "allow") for r in Risk}
        self.rules: list[Rule] = []
        self.budgets = {"external_per_day": 0, "writes_per_hour": 0}  # 0 = no limit
        self._mtime = 0.0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS grants (tool TEXT, source TEXT, until REAL)")
        self.reload()

    def set_ask_for(self, risks: frozenset) -> None:
        """APPROVAL_RISKS changed: the defaults follow, unless the policy file sets its own."""
        self.ask_for = risks
        if not self._mtime:
            self.defaults = {r.value: ("ask" if r in risks else "allow") for r in Risk}

    # ---------- the file ----------
    def reload(self) -> None:
        """Read the policy file if it changed (checked on every decision, so edits apply at once)."""
        if not self.path or not self.path.exists():
            return
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            defaults = {**self.defaults, **{k: str(v) for k, v in (data.get("defaults") or {}).items()}}
            rules = [Rule(**{k: v for k, v in r.items() if k in Rule.__dataclass_fields__})
                     for r in data.get("rules") or []]
            for item in [*defaults.values(), *(r.action for r in rules)]:
                if item not in ACTIONS:
                    raise ValueError(f"unknown action {item!r}")
        except (OSError, yaml.YAMLError, TypeError, ValueError) as e:
            log.error("policy file %s not loaded, keeping the previous one: %s", self.path, e)
            self._mtime = mtime
            return
        self.defaults, self.rules = defaults, rules
        self.budgets.update({k: int(v) for k, v in (data.get("budgets") or {}).items()})
        self._mtime = mtime
        log.info("policy loaded: %d rules, defaults %s, budgets %s", len(rules), defaults, self.budgets)

    # ---------- deciding ----------
    def decide(self, tool, args: dict, source: Source, audit=None, now: datetime | None = None) -> Decision:
        self.reload()
        now = now or datetime.now()
        if self.granted(tool.name, source):
            d = Decision("allow", "allowed for a while from an approval card", "grant")
        else:
            rule = next((r for r in self.rules if r.matches(tool, args, source, now)), None)
            if rule:
                d = Decision(rule.action, rule.why, f"rule {rule.tool}")
            elif tool.self_scoped and tool.risk is Risk.WRITE:
                d = Decision("allow", "", "self-scoped")  # JARVIS's own notes and journal
            else:
                d = Decision(self.defaults.get(tool.risk.value, "ask"), "", f"default {tool.risk.value}")
        if d.action == "allow" and d.rule != "grant" and getattr(tool, "confirm", ""):
            d = Decision("ask", tool.confirm, "confirm")  # the tool always asks (registry.Tool.confirm)
        if d.action == "allow" and audit is not None and (over := self._over_budget(tool, audit)):
            return Decision("deny", over, "budget")
        return d

    def _over_budget(self, tool, audit) -> str:
        if tool.risk is Risk.EXTERNAL and (cap := self.budgets.get("external_per_day")):
            if audit.count_since(time.time() - 86400, risk="external") >= cap:
                return f"the daily budget of {cap} outside calls is used up"
        if tool.risk in (Risk.WRITE, Risk.DESTRUCTIVE) and (cap := self.budgets.get("writes_per_hour")):
            if audit.count_since(time.time() - 3600, risk=tool.risk.value) >= cap:
                return f"the limit of {cap} changes an hour is reached"
        return ""

    # ---------- grants ("allow for 1 hour") ----------
    def grant(self, tool: str, source: str, minutes: float) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO grants VALUES (?,?,?)", (tool, source, time.time() + minutes * 60))

    def granted(self, tool: str, source: Source) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM grants WHERE tool=? AND source IN (?, '*') AND until>?",
                                    (tool, source.value, time.time())).fetchone() is not None

    def grants(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT tool, source, until FROM grants WHERE until>? ORDER BY until",
                                    (time.time(),)).fetchall()
        return [{"tool": t, "source": s, "until": u} for t, s, u in rows]

    def revoke(self, tool: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM grants WHERE tool=?", (tool,))

    def describe(self) -> dict:
        return {"file": str(self.path) if self.path and self.path.exists() else None, "defaults": self.defaults,
                "rules": [r.__dict__ for r in self.rules], "budgets": self.budgets, "grants": self.grants()}
