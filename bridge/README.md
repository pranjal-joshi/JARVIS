# Expert bridge (Claude Code / opencode)

Lets JARVIS hand a hard question to a stronger model through the coding CLIs installed on this
machine. The local 2b model calls the `ask_expert` tool; the hub posts the question to this bridge;
the bridge runs the CLI and returns the answer, which JARVIS shows as its reply.

```
HUD ──► hub (Docker) ──ask_expert──► [approval card: EXTERNAL] ──► bridge (host, 127.0.0.1:8790)
                                                                     ├─ claude -p   (your Claude plan)
                                                                     └─ opencode run (free models)
```

## Run it

From the repo root, in a terminal that stays open:

```bash
uv run --no-project python -I bridge/expert_bridge.py
```

It needs `EXPERT_BRIDGE_TOKEN` in `.env` (bootstrap or a random string) and listens on
`127.0.0.1:8790` only; containers reach it as `host.docker.internal`. Ctrl+C stops it. With the
bridge stopped, `ask_expert` just reports that it isn't reachable.

| Setting (`.env`) | Default | |
|---|---|---|
| `EXPERT_BRIDGE_TOKEN` | — | empty = tool not registered in the hub |
| `EXPERT_DEFAULT_ENGINE` | `claude` | or `opencode`; JARVIS can also pick per question |
| `EXPERT_CLAUDE_MODEL` | CLI default | e.g. `sonnet`, `opus` |
| `EXPERT_OPENCODE_MODEL` | `opencode/big-pickle` | `opencode models` lists them (`*-free` are free) |
| `EXPERT_TIMEOUT_S` | `150` | per call |

## Safety

- **Every call needs your approval** in the HUD (risk `external`, violet card "LEAVES THIS MACHINE").
  The card shows the exact question and context that will be sent. The voice satellite can't approve.
- **Answer-only.** Claude Code runs with `--tools ""`, `--strict-mcp-config`, no session persistence;
  opencode with `--pure` and a sandbox `opencode.json` that switches all tools and permissions off.
  Both run in an empty temp folder, so the expert can't read files or run commands.
- Bearer token, loopback only, one call at a time, size limits; logs hold metadata, never the text.
- What you send goes to Anthropic (Claude Code) or to the opencode provider. Free opencode models
  may log or train on prompts: don't approve anything private for them.
- On this work laptop don't point opencode at the `github-copilot/*` models (work licence).

## Limits

- Free opencode models have a usage quota; when it's used up the bridge returns
  "Free usage limit reached" quickly instead of hanging.
- Claude Code calls count against your Claude plan limits.
- The PowerShell script variant was blocked by this laptop's security policy, so use the `uv run` line.
