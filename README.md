# JARVIS: local-first home assistant (POC)

A local-first, voice + web AI assistant with tool access to the home (Home Assistant via MCP),
a Logseq knowledge base as long-term memory, and a holographic HUD. Everything runs in Docker;
nothing is exposed to the internet.

```
             ┌──────────── HA Assist (voice satellites) ────────────┐
             │  STT: whisper (Wyoming)   TTS: piper / tts-jarvis    │
             └───────────────┬──────────────────────────────────────┘
                             │ OpenAI-compatible /v1/chat/completions (token = source "satellite")
 browser HUD ──► ui (Next.js) ──► hub (FastAPI) ──► Ollama (qwen3.5:2b on GPU, nomic-embed-text)
   mic/TTS        server proxy     │  agent loop ─► tool registry ─► MCP: Home Assistant
                                   │  approval gate (web/phone approve, never voice)
                                   │  audit log (SQLite) ─► jarvis/daily/<date>
                                   └► Logseq graph (markdown) ◄─► Qdrant index
```

## Status

| Phase | What | State |
|---|---|---|
| 1 | Repo, compose, env, scripts | done |
| 2 | Hub agent + Ollama + HA MCP + approvals + audit | done and tested; HA link needs your setup ([docs/ha-setup.md](docs/ha-setup.md)) |
| 3 | Logseq file graph: search, jarvis/ memory, journal, Qdrant sync, daily summary, tasks | done (sample graph) |
| 4 | Wyoming STT/TTS, hub as HA agent, VoiceStudio cloned voice | hub side done; HA pipeline is your setup; cloned voice needs your clip |
| 5 | HUD v1: orb, chat, action timeline, approval cards, memory panel | done |
| 6–10 | Shell/filesystem/homelab MCP, audio, ESPHome, 3D graph, phone, Proxmox | not started |

See [docs/STATUS.md](docs/STATUS.md) for the detailed done/todo list.

## Quick start (WSL2 or any Linux with Docker)

```bash
scripts/wsl/bootstrap.sh          # creates .env with fresh tokens (never overwrites)
docker compose up -d --build      # files + profiles come from COMPOSE_FILE / COMPOSE_PROFILES in .env
scripts/wsl/pull-models.sh        # LLM + embedding model from .env (builds ollama/<name>.Modelfile names)
scripts/wsl/doctor.sh             # checks GPU, ports, services, hub status
```

Open http://localhost:3000. Type or press ◉ to talk (the browser asks for the mic).

| Service | Address | Notes |
|---|---|---|
| UI | http://localhost:3000 | loopback only |
| Hub | http://localhost:8700 | `/health`, `/v1/*` need a bearer token |
| Qdrant | http://localhost:6333 | loopback only |
| Ollama | http://localhost:11434 | loopback only |
| VoiceStudio | http://localhost:3900 | only with `--profile voicestudio` |
| Wyoming | whisper:10300, piper:10200, tts-jarvis:10210 | no host ports; HA reaches them via `compose.ha.yaml` |

## Compose files and profiles

| Command | Starts |
|---|---|
| `docker compose up -d` | hub, qdrant, ollama, ui |
| `--profile voice` | + whisper, piper |
| `--profile voicestudio` | + VoiceStudio and its Wyoming bridge (cloned voice) |
| `-f compose.yaml -f compose.ha.yaml` | joins the existing `ha_network` so HA can reach hub + Wyoming |
| `-f compose.yaml -f compose.gpu.yaml` | NVIDIA hosts with a real GPU (homelab): Ollama, Whisper, VoiceStudio on GPU |
| `-f compose.yaml -f compose.gpu-llm.yaml` | small GPU (this laptop's 2 GB MX550): only Ollama on GPU; set as the default here via `COMPOSE_FILE` in `.env` |

### Models on this laptop

`LLM_MODEL=jarvis-2b-gpu` is qwen3.5:2b (4-bit) with every layer forced onto the GPU
([ollama/jarvis-2b-gpu.Modelfile](ollama/jarvis-2b-gpu.Modelfile)); Ollama's auto-fit would otherwise leave it on CPU.
It uses ~1.4 of the ~1.5 GB free, so STT, TTS and embeddings (`jarvis-embed-cpu`) stay on CPU.
Measured: 2–4 s per reply with tool lookups, against 11–57 s for qwen3.5:4b on CPU. The 2b model is
weaker at tool judgement; the hub catches replies that fake a tool call as text and never reports them as done.

## Expert uplink (optional)

For hard questions JARVIS can consult Claude Code or opencode on this machine (`ask_expert`).
Run `uv run --no-project python -I bridge/expert_bridge.py` on the host; each question needs your
approval in the HUD and the expert runs answer-only, with no tools. See [bridge/README.md](bridge/README.md).

## Security model

- **Source by token.** Each client has its own bearer token (`HUB_TOKEN_SATELLITE/WEB/PHONE`). The token,
  not the request body, decides the source; replies and events go only to that source.
- **Allow-listed tools.** MCP tools are hidden unless matched in [mcp/servers.yaml](mcp/servers.yaml),
  each tagged `read`, `write` or `destructive`. Server annotations can raise risk, never lower it.
- **Approval gate.** `write`/`destructive` tools wait for Approve/Deny in the HUD (or phone). The satellite
  token can never approve, so voice alone never triggers a write. Unapproved calls time out (90 s) and are not run.
- **Agent memory scope.** The agent writes freely only to `jarvis/*` pages and the journal; editing any of
  your other pages is a `write` that needs approval.
- **Untrusted content.** Tool results and KB text are fenced and labelled as data; the sample graph
  includes an injection page ([[Untrusted Clipping]]) to test this.
- **Exposure.** Every host port binds to 127.0.0.1. Remote access is meant to go through Tailscale (later phase).
- **Audit.** Every tool call (including denied/timed-out ones) is in the SQLite audit log (`/v1/audit`).
- **Files and PowerShell on the laptop** (device bridge). `read_file`/`list_files` only read: folders listed in
  `DEVICE_FILES_ALLOW`, or `*` for every local drive; credential-looking files (`.env`, keys, `.ssh`, password
  stores) stay unreadable. `powershell_read` runs look-only commands (or a named check such as `disk_space`)
  without asking; `powershell_run` asks every time. [bridge/ps_classify.ps1](bridge/ps_classify.ps1) checks each
  command with PowerShell's parser and refuses file writes, moves and deletes, nested shells and `iex`. The output
  of both counts as outside text (taint).
- **Restarts.** Timers (`timers.json`), approvals, grants and skill schedules (`skills.json`) are kept on disk.
  A timer that came due while the hub was down rings at start-up, marked late; a daily skill missed by under 6 h runs late.

## Layout

```
hub/        FastAPI agent service (Python 3.13, uv)       hub/tests: 19 tests
ui/         Next.js 16 HUD (PWA)
kb/         sample Logseq graph (file-based)
mcp/        servers.yaml allow-lists
voice/      VoiceStudio profile notes, reference clip folder (git-ignored)
esphome/    satellite YAML (Phase 7)
audio/      Music Assistant + Sendspin (Phase 7)
scripts/    wsl/ helper scripts
docs/       HA setup, status, decisions, test guides
```

## Development

```bash
cd hub && uv sync && uv run pytest        # hub tests (no Docker needed)
cd ui && npm ci && npm run dev            # HUD against a hub on :8700 (set HUB_TOKEN_WEB)
```
