# Testing phases 1–5

Run from the repo root in WSL (or Git Bash). Load tokens first:

```bash
set -a; source .env; set +a
H="Authorization: Bearer $HUB_TOKEN_WEB"
```

## Automated

```bash
cd hub && uv run pytest -q        # 19 tests: auth/source routing, events, approvals, audit, KB, tickets
```

## Phase 1: stack

```bash
docker compose --profile voice ps                 # all Up; hub (healthy)
curl -s localhost:8700/health                     # {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8700/v1/models   # 401 without a token
scripts/wsl/doctor.sh
```

## Phase 2: agent, approvals, audit

```bash
curl -s -H "$H" localhost:8700/v1/status | python3 -m json.tool    # llm up, tools list, mcp status
curl -s -H "$H" -H 'Content-Type: application/json' localhost:8700/v1/chat/completions \
  -d '{"model":"jarvis","messages":[{"role":"user","content":"Good evening. Who are you?"}]}'
```

In the HUD (http://localhost:3000):

1. *"Note in my journal that I tested JARVIS tonight."* → runs without a card (own journal),
   appears in the timeline and memory panel.
2. *"Add 'call the plumber' to my Errands page."* → **approval card** (editing your page). Deny → nothing
   is written and the timeline shows `denied`. Ask again and Approve → the line appears in
   `data/kb-graph/pages/Errands.md`.
3. With HA connected ([ha-setup.md](ha-setup.md)): *"Turn on the desk lamp."* → card → Approve → lamp on.
4. `curl -s -H "$H" localhost:8700/v1/audit | python3 -m json.tool` shows every call above.
5. The satellite token can't approve: `curl -s -X POST -H "Authorization: Bearer $HUB_TOKEN_SATELLITE" localhost:8700/v1/approvals/x -d '{"approved":true}' -H 'Content-Type: application/json'` → 403.

## Phase 3: knowledge base

```bash
curl -s -H "$H" -X POST localhost:8700/v1/kb/sync                     # "vector": true once nomic-embed-text is pulled
curl -s -H "$H" "localhost:8700/v1/kb/search?q=evening%20drink"        # jarvis/preferences first
curl -s -H "$H" localhost:8700/v1/kb/tasks                            # Renew car insurance (2026-11-03), NVMe (deadline)
curl -s -H "$H" -X POST localhost:8700/v1/kb/daily-summary            # writes jarvis/daily/<today>
```

HUD prompts: *"What are my open tasks?"*, *"Remember that I like the study at 22 degrees."*
(→ new `jarvis/memory` block), *"What does my Untrusted Clipping page say?"* (it should report the
injected instruction, not obey it).

Open `data/kb-graph` as a graph in the Logseq desktop app to see JARVIS's pages and journal entries;
the memory panel links (`logseq://...`) open them directly.

## Phase 4: voice

```bash
# Piper TTS -> Whisper STT round trip through the hub (needs the voice models downloaded)
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"input":"Good evening, sir."}' \
  localhost:8700/v1/audio/speech -o /tmp/t.wav -D - | grep -i x-tts-engine
curl -s -H "$H" -F "file=@/tmp/t.wav;type=audio/wav" localhost:8700/v1/audio/transcriptions
```

In the HUD: press ◉, speak, press ■. The transcript is sent as a chat message and the reply is spoken.
HA Assist pipeline: [ha-setup.md](ha-setup.md) §3–5. Cloned voice: [voice/README.md](../voice/README.md).

## Phase 5: HUD

- Orb changes colour/motion for listening (cyan), thinking (amber pulse), speaking (reacts to audio).
- Header chips: link online, LLM up, kb vector, tts engine, tool count.
- Timeline lists tool calls, results, KB recalls (◈) and past audit rows.
- Approval cards appear for write actions and disappear after a decision.
- Memory panel lists `jarvis/` and `#jarvis` journal blocks with Logseq deep links.
- Narrow the window below 1000 px for the stacked mobile layout; OS "reduce motion" freezes the orb.
