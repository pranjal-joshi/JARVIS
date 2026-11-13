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

A hook can also have its own secret (made in the HUD, kept in webhooks.json): then only that secret opens it,
and the shared one does not, so a secret handed to GitHub can't fire the doorbell skill.

Replays: a caller that signs can add `X-Jarvis-Timestamp: <unix seconds>` and send
`X-Jarvis-Signature: sha256=<HMAC-SHA256 of "<timestamp>.<raw body>">`; a timestamp more than 5 minutes off is
refused. A delivery id (X-GitHub-Delivery, X-Jarvis-Delivery, X-Request-Id...) seen in the last day is answered
200 "duplicate" and runs nothing, so a retried or replayed delivery fires a skill once.

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
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jarvis_hub.api.auth import require_source
from jarvis_hub.approval.gate import APPROVER_SOURCES
from jarvis_hub.config import Settings, Source
from jarvis_hub.files import write_atomic

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")

MAX_BODY = 64 * 1024
TEXT_MAX = 400
PER_HOOK = 20     # calls per hook name per minute
PER_MINUTE = 60   # calls across all hooks per minute
LOG_MAX = 40
BAD_PER_MINUTE = 10  # failed attempts logged per minute; past this they are refused unlogged (no log flood)
SKEW_S = 300         # a signed timestamp may be this far from now
SEEN_S = 24 * 3600   # delivery ids remembered this long
SEEN_MAX = 2000
DELIVERY_HEADERS = ("x-github-delivery", "x-jarvis-delivery", "x-request-id", "x-vss-deliveryid",
                    "x-gitlab-event-uuid", "idempotency-key")
NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
# fields that usually say what happened, tried in this order (GitHub, Azure DevOps, HA, IFTTT, generic)
PICK = ("title", "subject", "message", "text", "summary", "detailedMessage", "action", "status", "state",
        "event", "eventType", "conclusion", "ref")
NESTED = ("pull_request", "issue", "workflow_run", "check_run", "release", "resource", "repository",
          "head_commit", "comment", "data")
BARE = frozenset({"title", "subject", "message", "text", "summary"})  # alone, these are the whole news
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
        data = self._load()
        self._secret = env if self.from_env else str(data.get("secret") or "")
        hooks = data.get("hooks")
        self.per_hook: dict[str, str] = {k: str(v) for k, v in hooks.items() if v} if isinstance(hooks, dict) else {}
        self.limiter = Limiter()
        self.bad: deque = deque()  # when recent failed attempts came
        self.seen: dict[str, float] = {}  # delivery id -> when
        self.log: deque = deque(maxlen=LOG_MAX)

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            write_atomic(self.state_file, json.dumps({"secret": "" if self.from_env else self._secret,
                                                   "hooks": self.per_hook}), encoding="utf-8")
        except OSError as e:
            log.warning("webhook secret not saved: %s", e)

    def secret_for(self, name: str) -> str:
        """The secret that opens this hook: its own when it has one, else the shared one."""
        return self.per_hook.get(name) or self._secret

    def is_open(self, name: str) -> bool:
        return bool(self.secret_for(name))

    def rotate_hook(self, name: str) -> str:
        self.per_hook[name] = secrets.token_urlsafe(32)
        self._save()
        return self.per_hook[name]

    def drop_hook(self, name: str) -> None:
        self.per_hook.pop(name, None)
        self._save()

    def bad_allowed(self, now: float | None = None) -> bool:
        """A failed attempt may be logged (False: too many lately; refuse it without logging)."""
        now = time.monotonic() if now is None else now
        while self.bad and now - self.bad[0] > 60:
            self.bad.popleft()
        self.bad.append(now)
        return len(self.bad) <= BAD_PER_MINUTE

    def duplicate(self, delivery: str, now: float | None = None) -> bool:
        """Has this delivery id been seen in the last day (and remember it)."""
        if not delivery:
            return False
        now = time.time() if now is None else now
        if len(self.seen) > SEEN_MAX:
            old = [k for k, t in self.seen.items() if now - t > SEEN_S] or list(self.seen)[:len(self.seen) // 2]
            for k in old:
                self.seen.pop(k, None)
        if delivery in self.seen and now - self.seen[delivery] <= SEEN_S:
            return True
        self.seen[delivery] = now
        return False

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


def authorized(secret: str, headers, body: bytes, now: float | None = None) -> bool:
    if not secret:
        return False
    if ts := headers.get("x-jarvis-timestamp"):  # signed with a timestamp: fresh, and the body untouched
        try:
            fresh = abs((time.time() if now is None else now) - float(ts)) <= SKEW_S
        except ValueError:
            return False
        sig = headers.get("x-jarvis-signature", "").strip()
        want = "sha256=" + hmac.new(secret.encode(), ts.strip().encode() + b"." + body, hashlib.sha256).hexdigest()
        return fresh and hmac.compare_digest(sig.encode(), want.encode())
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
    picked = [k for k in PICK if isinstance(payload.get(k), (str, int, float)) and str(payload[k]).strip()]
    nested = [k for k in NESTED if isinstance(payload.get(k), dict)]
    if len(picked) == 1 and not nested and picked[0] in BARE:
        # {"message": "Amazon person at the door"} -> the words alone: "message=" only invites copying the line
        return " ".join(str(payload[picked[0]]).split())[:TEXT_MAX]
    parts = [f"{k}={_short(payload[k])}" for k in picked]
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


async def _read_body(request: Request) -> bytes | None:
    """The body, or None once it passes MAX_BODY (read in pieces: a missing or false Content-Length can't make
    the hub hold a huge body in memory)."""
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return None
    except ValueError:
        raise HTTPException(400, "bad Content-Length") from None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return None
    return bytes(body)


def _refuse(hooks: "Webhooks", name: str, status: int, note: str, detail: str) -> HTTPException:
    """A failed attempt: logged while there are few; a flood of them is refused unlogged."""
    if hooks.bad_allowed():
        hooks.record(name, status, note)
        return HTTPException(status, detail)
    return HTTPException(429, "too many failed calls; try again in a minute")


@router.post("/hooks/{name}", status_code=202)
async def hook(name: str, request: Request):
    hooks: Webhooks = request.app.state.hooks
    name = _clean_name(name)
    if not hooks.is_open(name):
        raise HTTPException(404, "not found")
    body = await _read_body(request)
    if body is None:
        raise _refuse(hooks, name, 413, "body too large", "body too large")
    if not authorized(hooks.secret_for(name), request.headers, body):
        raise _refuse(hooks, name, 401, "bad secret, signature or timestamp", "bad webhook secret or signature")
    delivery = next((f"{h}:{request.headers[h].strip()}" for h in DELIVERY_HEADERS if request.headers.get(h)), "")
    if hooks.duplicate(delivery):
        hooks.record(name, 200, "duplicate delivery: ignored")
        return JSONResponse({"ok": True, "hook": name, "duplicate": True, "skills": []}, status_code=200)
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
        # hooks with their own secret (the secret is shown once, when made)
        "own_secrets": {k: f"…{v[-4:]}" for k, v in sorted(hooks.per_hook.items())},
        "log": list(hooks.log),
        "limits": {"per_hook_per_min": PER_HOOK, "per_min": PER_MINUTE, "max_body": MAX_BODY},
    }


class SecretBody(BaseModel):
    action: str  # rotate | disable
    hook: str = ""  # a hook's own secret; empty: the shared one


@router.post("/webhooks/secret")
async def webhooks_secret(body: SecretBody, request: Request, _: Source = Depends(require_approver)) -> dict:
    hooks: Webhooks = request.app.state.hooks
    if body.hook:
        name = _clean_name(body.hook)
        if body.action == "rotate":
            return {"hook": name, "secret": hooks.rotate_hook(name)}
        if body.action == "disable":  # back to the shared secret
            hooks.drop_hook(name)
            return {"hook": name, "secret": None}
        raise HTTPException(400, "action must be rotate or disable")
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
