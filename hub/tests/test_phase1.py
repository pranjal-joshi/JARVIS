import pytest

from conftest import auth


def chat(client, token, text="hello", **extra):
    return client.post("/v1/chat/completions", headers=auth(token), json={
        "model": "jarvis", "messages": [{"role": "user", "content": text}], **extra})


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_chat_rejects_unknown_and_placeholder_tokens(client):
    assert chat(client, "nope").status_code == 401
    assert chat(client, "change-me").status_code == 401


def test_token_decides_source(client):
    r = chat(client, "sat-token", "lights on")
    assert r.status_code == 200
    body = r.json()
    assert body["jarvis"]["source"] == "satellite"
    assert "lights on" in body["choices"][0]["message"]["content"]


def test_streaming_response(client):
    r = chat(client, "web-token", "ping", stream=True)
    assert r.status_code == 200
    lines = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    assert any("ping" in ln for ln in lines)


def test_events_only_reach_own_source(client):
    with client.websocket_connect("/v1/events") as web:
        web.send_json({"token": "web-token"})
        assert web.receive_json()["source"] == "web"
        chat(client, "sat-token")  # satellite request: web subscriber must not see it
        chat(client, "web-token", "ping")
        first = web.receive_json()
        assert (first["source"], first["type"]) == ("web", "thinking")
        assert web.receive_json()["data"]["text"].endswith("ping")


def test_events_reject_bad_token(client):
    with client.websocket_connect("/v1/events") as ws:
        ws.send_json({"token": "nope"})
        with pytest.raises(Exception):
            ws.receive_json()


def test_satellite_cannot_use_approver_endpoints(client):
    assert client.get("/v1/audit", headers=auth("sat-token")).status_code == 403
    assert client.get("/v1/approvals", headers=auth("sat-token")).status_code == 403
    assert client.get("/v1/audit", headers=auth("web-token")).status_code == 200


def test_ws_ticket_is_one_time(client):
    t = client.post("/v1/events/ticket", headers=auth("web-token")).json()["ticket"]
    with client.websocket_connect("/v1/events") as ws:
        ws.send_json({"ticket": t})
        assert ws.receive_json()["source"] == "web"
    with client.websocket_connect("/v1/events") as ws:
        ws.send_json({"ticket": t})
        with pytest.raises(Exception):
            ws.receive_json()
