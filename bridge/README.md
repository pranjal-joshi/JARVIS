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

# Device bridge (this laptop + Android over ADB)

`device_bridge.py` gives the hub the `win_*` and `adb_*` tools. Same pattern as the expert bridge:
host-only (127.0.0.1:8791), bearer token, standard library only.

```
uv run --no-project python bridge/device_bridge.py
```

Settings in `.env`: `DEVICE_BRIDGE_TOKEN`, `ADB_PATH` (portable platform-tools is fine),
`DEVICE_ADB_ALLOW` (hardware serials JARVIS may touch), `DEVICE_WIN_APPS` (`name=target` apps it may open).

## Pair a phone over Wi-Fi (Android 11+)

The phone and laptop must be on the same LAN. ADB runs on the laptop; the hub only talks to the bridge.

1. Phone: Settings → About phone → tap *Build number* 7 times (developer options).
2. Phone: Developer options → **Wireless debugging** → on → **Pair device with pairing code**.
   It shows `IP:PORT` and a 6-digit code (this pairing port is different from the connect port).
3. Laptop: `adb pair 192.168.0.42:37123` and type the code. Pairing is remembered.
4. Laptop: `adb connect 192.168.0.42:41567` with the `IP:PORT` shown on the main Wireless debugging screen.
5. Ask JARVIS "list android devices" (or `adb devices -l` plus `adb -s <id> shell getprop ro.serialno`),
   copy the device's **serial** into `DEVICE_ADB_ALLOW` in `.env`, restart the bridge.

The connect port changes when wireless debugging restarts: re-run step 4 or ask JARVIS to
"reconnect the phone at IP:PORT" (`adb_connect`). The allow-list uses the hardware serial, so it survives that.

## Risk levels

| Tools | Risk | Approval |
|---|---|---|
| `win_info`, `win_screenshot`, `adb_devices`, `adb_info`, `adb_screenshot` | read | no |
| `win_media`, `win_open`, `win_lock`, `adb_open_app`, `adb_key`, `adb_connect` | write | no (audited) |
| `adb_tap`, `adb_type`, `adb_shell`, `adb_install`, `adb_uninstall` | destructive | yes, each time |

No Windows shell passthrough; apps open only from `DEVICE_WIN_APPS`; APKs install only from `data/apk/`.
Screenshots land in `data/screenshots/` (git-ignored).
