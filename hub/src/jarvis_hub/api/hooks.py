"""Inbound webhooks: POST /v1/hooks/<name> fires `trigger:: webhook` skills.

Anything that can send an HTTP request can start a skill this way: a Home Assistant automation
(rest_command), GitHub or Azure DevOps service hooks, IFTTT/Zapier, a cron job, a script.

The caller proves it knows the webhook secret, either
  - as a header: `X-Jarvis-Token: <secret>` or `Authorization: Bearer <secret>`, or
  - GitHub-style: `X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body>`.
The secret is HUB_WEBHOOK_SECRET when set (then the HUD can't change it), else one made in the HUD's
webhooks panel and kept in webhooks.json next to the hub database. No secret = the endpoint answers 404.
It is its own secret, not a hub client token: a webhook can start skills and nothing else (no chat, no
approvals), so it can be handed to an outside service without handing it the HUD.

The body (JSON or text) is someone else's words: a skill sees it fenced as data, never as instructions,
and writes or sends still wait for approval as on any skill run. Each hook is rate-limited, and the last
deliveries (accepted or refused, never the body) are kept for the HUD."""

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jarvis_hub.api.auth import require_source
from jarvis_hub.approval.gate import APPROVER_SOURCES
from jarvis_hub.config import Settings, Source

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")

MAX_BODY = 64 * 1024
TEXT_MAX = 400
PER_HOOK = 20     # calls per hook name per minute
PER_MINUTE = 60   # calls across all hooks per minute
LOG_MAX = 40
NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
# fields that usually say what happened, tried in this order (GitHub, Azure DevOps, HA, IFTTT, generic)
PICK = ("title", "subject", "message", "text", "summary", "detailedMessage", "action", "status", "state",
        "event", "eventType", "conclusion", "ref")
NESTED = ("pull_request", "issue", "workflow_run", "check_run", "release", "resource", "repository",
          "head_commit", "comment", "data")
EVENT_HEADERS = ("x-github-event", "x-event-key", "x-gitlab-event", "x-jarvis-event")


class Limiter:
    def __init__(self) -> None:
        self.calls: dict[str, deque] = {}

    def allow(self, name: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        everyone = self.calls.setdefault("", deque())
        mine = self.calls.setdefault(name, deque())
        for q in (everyone, mine):
            while q and now - q[0] > 60:
                q.popleft()
        if len(everyone) >= PER_MINUTE or len(mine) >= PER_HOOK:
            return False
        everyone.append(now)
        mine.append(now)
        return True


class Webhooks:
    """The secret (env or HUD-made), the rate limiter and the delivery log."""

    def __init__(self, settings: Settings, state_file: Path) -> None:
        self.state_file = state_file
        env = settings.hub_webhook_secret.get_secret_value().strip()
        self.from_env = env not in ("", "change-me")
        self._secret = env if self.from_env else self._load()
        self.limiter = Limiter()
        self.log: deque = deque(maxlen=LOG_MAX)

    def _load(self) -> str:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return str(data.get("secret") or "") if isinstance(data, dict) else ""
        except (OSError, ValueError):
            return ""

    def _save(self) -> None:
        try:
            self.state_file.write_text(json.dumps({"secret": self._secret}), encoding="utf-8")
        except OSError as e:
            log.warning("webhook secret not saved: %s", e)

    @property
    def secret(self) -> str:
        return self._secret

    @property
    def mode(self) -> str:
        return "env" if self.from_env else ("hud" if self._secret else "off")

    def rotate(self) -> str:
        if self.from_env:
            raise PermissionError("the secret is set by HUB_WEBHOOK_SECRET; change it there")
        self._secret = secrets.token_urlsafe(32)
        self._save()
        return self._secret

    def disable(self) -> None:
        if self.from_env:
            raise PermissionError("the secret is set by HUB_WEBHOOK_SECRET; clear it there")
        self._secret = ""
        self._save()

    def record(self, name: str, status: int, note: str = "", skills: list[str] | None = None,
               test: bool = False) -> None:
        self.log.appendleft({"ts": time.time(), "hook": name, "status": status, "note": note[:200],
                             "skills": skills or [], "test": test})


def authorized(secret: str, headers, body: bytes) -> bool:
    if not secret:
        return False
    if sig := headers.get("x-hub-signature-256"):
        want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.strip().encode(), want.encode())
    given = headers.get("x-jarvis-token", "")
    auth = headers.get("authorization", "")
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:]
    return bool(given) and secrets.compare_digest(given.strip().encode(), secret.encode())


def _short(v) -> str:
    return " ".join(str(v).split())[:160]


def summarize(payload) -> str:
    """One line saying what happened, from the fields services usually use; else compact JSON."""
    if isinstance(payload, str):
        return " ".join(payload.split())[:TEXT_MAX]
    if not isinstance(payload, dict):
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)[:TEXT_MAX]
    parts = [f"{k}={_short(payload[k])}" for k in PICK if isinstance(payload.get(k), (str, int, float))
             and str(payload[k]).strip()]
    for key in NESTED:
        sub = payload.get(key)
        if isinstance(sub, dict):
            for k in ("title", "name", "full_name", "message", "conclusion", "status"):
                if isinstance(sub.get(k), (str, int)) and str(sub[k]).strip():
                    parts.append(f"{key}.{k}={_short(sub[k])}")
                    break
    text = "; ".join(parts) if parts else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return text[:TEXT_MAX]


def _clean_name(name: str) -> str:
    name = name.strip().lower()
    if not 0 < len(name) <= 40 or not set(name) <= NAME_OK:
        raise HTTPException(400, "hook name: letters, digits, _ and - (at most 40)")
    return name


def _fire(request: Request, name: str, body: bytes, event: str, test: bool = False) -> dict:
    text_body = body.decode("utf-8", "replace")
    try:
        payload = json.loads(text_body) if text_body.strip() else {}
    except ValueError:
        payload = text_body
    event = _short(event)[:60]
    what = summarize(payload)
    text = f"{name}" + (f" ({event})" if event else "") + (f": {what}" if what else "")
    s = request.app.state
    fired = s.skills.emit("webhook", text, {"hook": name, "event": event, "size": len(body),
                                            "summary": what, "test": test})
    s.hooks.record(name, 202, what, fired, test)
    return {"ok": True, "hook": name, "text": text, "skills": fired}


@router.post("/hooks/{name}", status_code=202)
async def hook(name: str, request: Request) -> dict:
    hooks: Webhooks = request.app.state.hooks
    if not hooks.secret:
        raise HTTPException(404, "not found")
    name = _clean_name(name)
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        hooks.record(name, 413, "body too large")
        raise HTTPException(413, "body too large")
    body = await request.body()
    if len(body) > MAX_BODY:
        hooks.record(name, 413, "body too large")
        raise HTTPException(413, "body too large")
    if not authorized(hooks.secret, request.headers, body):
        hooks.record(name, 401, "bad secret or signature")
        raise HTTPException(401, "bad webhook secret or signature")
    if not hooks.limiter.allow(name):
        hooks.record(name, 429, "rate limited")
        raise HTTPException(429, "too many calls to this hook; try again in a minute")
    event = next((request.headers[h] for h in EVENT_HEADERS if request.headers.get(h)), "")
    return _fire(request, name, body, event)


# ---------- HUD management (approver sources only) ----------
def require_approver(source: Source = Depends(require_source)) -> Source:
    if source not in APPROVER_SOURCES:
        raise HTTPException(403, "only the web UI or phone can manage webhooks")
    return source


@router.get("/webhooks")
async def webhooks_state(request: Request, reveal: bool = False, _: Source = Depends(require_approver)) -> dict:
    st = request.app.state
    hooks: Webhooks = st.hooks
    skills = [s for s in st.skills.skills() if s["trigger"] == "webhook"]
    names = sorted({s["match"].lower() for s in skills if s["match"]} | {e["hook"] for e in hooks.log})
    secret = hooks.secret
    return {
        "mode": hooks.mode,  # env | hud | off
        "secret": secret if reveal and secret else None,
        "hint": f"…{secret[-4:]}" if secret else "",
        "skills": [{"name": s["name"], "match": s["match"], "enabled": s["enabled"],
                    "last_run": s["last_run"]} for s in skills],
        "hooks": names,
        "log": list(hooks.log),
        "limits": {"per_hook_per_min": PER_HOOK, "per_min": PER_MINUTE, "max_body": MAX_BODY},
    }


class SecretBody(BaseModel):
    action: str  # rotate | disable


@router.post("/webhooks/secret")
async def webhooks_secret(body: SecretBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    hooks: Webhooks = request.app.state.hooks
    try:
        if body.action == "rotate":
            return {"mode": hooks.mode, "secret": hooks.rotate()}
        if body.action == "disable":
            hooks.disable()
            return {"mode": hooks.mode, "secret": None}
    except PermissionError as e:
        raise HTTPException(409, str(e)) from None
    raise HTTPException(400, "action must be rotate or disable")


class TestBody(BaseModel):
    name: str
    body: str = ""
    event: str = "test"


@router.post("/webhooks/test")
async def webhooks_test(body: TestBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    """Fire a hook from the HUD without the secret: the same skill runs a real call would start."""
    raw = body.body.encode()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "body too large")
    return _fire(request, _clean_name(body.name), raw, body.event, test=True)
