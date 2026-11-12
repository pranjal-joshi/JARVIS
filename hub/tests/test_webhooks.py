"""Inbound webhooks: POST /v1/hooks/<name> fires `trigger:: webhook` skills; the HUD manages the secret."""

import hashlib
import hmac
import json

from conftest import auth

from jarvis_hub.api.hooks import Limiter, PER_HOOK, summarize


def _skill(client, name="skills/Deploy done", match="github"):
    g = client.app.state.kb_graph
    g.append_to_user_page(name, f"trigger:: webhook\nmatch:: {match}\n\n- Tell me what was deployed.")
    # written outside the skill tools: it waits as "changed" until approved in the HUD
    assert client.app.state.skills.skills()[0]["changed"]
    assert client.post(f"/v1/skills/{name}/enable", json={"enabled": True}, headers=auth("web-token")).status_code == 200


def test_off_until_a_secret_exists(client):
    assert client.post("/v1/hooks/github", json={}).status_code == 404


def test_secret_from_the_hud_then_calls(client):
    r = client.post("/v1/webhooks/secret", json={"action": "rotate"}, headers=auth("web-token"))
    secret = r.json()["secret"]
    assert r.status_code == 200 and len(secret) > 20
    _skill(client)

    assert client.post("/v1/hooks/github", json={"title": "x"}).status_code == 401
    assert client.post("/v1/hooks/github", json={}, headers={"X-Jarvis-Token": "nope"}).status_code == 401
    # a hub client token is not the webhook secret
    assert client.post("/v1/hooks/github", json={}, headers=auth("web-token")).status_code == 401

    r = client.post("/v1/hooks/github", json={"action": "closed", "pull_request": {"title": "Fix login"}},
                    headers={"X-Jarvis-Token": secret, "X-GitHub-Event": "pull_request"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["skills"] == ["skills/Deploy done"]
    assert body["text"] == "github (pull_request): action=closed; pull_request.title=Fix login"

    # another hook name doesn't run the github skill, even when its body says github
    r = client.post("/v1/hooks/ha", content="github", headers=auth(secret))
    assert r.status_code == 202 and r.json()["skills"] == []

    state = client.get("/v1/webhooks", headers=auth("web-token")).json()
    assert state["mode"] == "hud" and state["secret"] is None and state["hint"] == "…" + secret[-4:]
    assert {"github", "ha"} <= set(state["hooks"])
    assert [e["status"] for e in state["log"][:3]] == [202, 202, 401]
    assert client.get("/v1/webhooks?reveal=true", headers=auth("web-token")).json()["secret"] == secret

    # only approver sources manage it
    assert client.get("/v1/webhooks", headers=auth("sat-token")).status_code == 403

    client.post("/v1/webhooks/secret", json={"action": "disable"}, headers=auth("web-token"))
    assert client.post("/v1/hooks/github", json={}, headers=auth(secret)).status_code == 404


def test_github_signature(client):
    secret = client.post("/v1/webhooks/secret", json={"action": "rotate"}, headers=auth("web-token")).json()["secret"]
    raw = json.dumps({"ref": "refs/heads/main"}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    ok = client.post("/v1/hooks/github", content=raw, headers={"X-Hub-Signature-256": sig})
    assert ok.status_code == 202
    bad = client.post("/v1/hooks/github", content=raw + b" ", headers={"X-Hub-Signature-256": sig})
    assert bad.status_code == 401


def test_env_secret_wins_and_cant_be_rotated(env):
    env.setenv("HUB_WEBHOOK_SECRET", "from-env-secret")
    from fastapi.testclient import TestClient

    from jarvis_hub.main import app
    with TestClient(app) as c:
        assert c.post("/v1/hooks/x", json={}, headers={"X-Jarvis-Token": "from-env-secret"}).status_code == 202
        assert c.post("/v1/webhooks/secret", json={"action": "rotate"}, headers=auth("web-token")).status_code == 409
        assert c.get("/v1/webhooks", headers=auth("web-token")).json()["mode"] == "env"


def test_test_button_and_bad_names(client):
    _skill(client, match="doorbell")
    r = client.post("/v1/webhooks/test", json={"name": "doorbell", "body": '{"message":"ding"}'},
                    headers=auth("web-token"))
    assert r.status_code == 200 and r.json()["skills"] == ["skills/Deploy done"]
    assert client.post("/v1/webhooks/test", json={"name": "../x"}, headers=auth("web-token")).status_code == 400


def test_limits_and_summary():
    lim = Limiter()
    assert all(lim.allow("a", now=0) for _ in range(PER_HOOK))
    assert not lim.allow("a", now=1)
    assert lim.allow("b", now=1)
    assert lim.allow("a", now=61)
    assert summarize("  hello\n world ") == "hello world"
    assert summarize({"zzz": 1}) == '{"zzz":1}'
    # a lone message is the whole news: no "message=" for the model to copy into a notification
    assert summarize({"message": "Amazon person at the door"}) == "Amazon person at the door"
    assert summarize({"message": "hi", "status": "ok"}) == "message=hi; status=ok"
    assert summarize({"eventType": "build.complete", "resource": {"status": "succeeded"}}) == \
        "eventType=build.complete; resource.status=succeeded"
