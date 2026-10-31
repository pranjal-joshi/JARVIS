# Connecting Home Assistant to JARVIS

Nothing here edits the ha-docker stack's files or containers. JARVIS only *joins* the existing
`ha_network` (via `compose.ha.yaml`) so HA and the hub can reach each other by container name.
Every step below is done by you, in the HA web UI.

There are two independent links:

| Direction | What | Lets you |
|---|---|---|
| hub → HA | HA's **Model Context Protocol Server** | JARVIS controls lights etc. (with approval) |
| HA → hub | **Local OpenAI LLM** (`local_openai`, already installed) pointed at the hub | HA Assist / voice satellites talk to JARVIS |
| HA → Wyoming | **Wyoming Protocol** integrations | Whisper STT, Piper / cloned-voice TTS for satellites |

## 1. Start JARVIS on HA's network

```bash
docker compose -f compose.yaml -f compose.ha.yaml --profile voice up -d
```

## 2. Hub → HA (MCP), so JARVIS can control devices

1. **Create a non-admin user for JARVIS.** Settings → People → Add person → name `JARVIS`,
   *Allow login* on, *Administrator* **off**.
2. Log in as that user (private window), open its profile → **Security** → *Long-lived access tokens* →
   Create token → copy it. Log out.
3. As yourself: Settings → Devices & services → **Add integration** → *Model Context Protocol Server* →
   LLM API: **Assist**.
4. Choose what JARVIS can see: Settings → Voice assistants → **Expose** tab. Expose only the entities
   you want (e.g. the desk lamp to start). Unexposed entities are invisible to JARVIS.
5. Put the token in `JARVIS/.env`:
   ```
   HA_TOKEN=<the token>
   ```
6. Restart the hub and check it connected:
   ```bash
   docker compose -f compose.yaml -f compose.ha.yaml --profile voice up -d hub
   scripts/wsl/doctor.sh      # look for: homeassistant: connected: N/M tools allowed
   ```
7. Test in the HUD (http://localhost:3000): *"Turn on the desk lamp."* An approval card appears;
   press **Approve** and the lamp turns on. The action shows in the timeline and in `/v1/audit`.

Which HA tools are visible, and their risk level, is set in [mcp/servers.yaml](../mcp/servers.yaml).
Locks, covers and alarms are not listed, so they stay hidden. Note that HA's `HassTurnOn/Off` also
work on locks if you expose one; every such call still needs approval.

## 3. HA → hub, so Assist uses JARVIS as its brain

1. Settings → Devices & services → **Add integration** → *Local OpenAI LLM*.
   - Base URL: `http://hub:8700/v1`
   - API key: the value of `HUB_TOKEN_SATELLITE` in `JARVIS/.env`
   - Model: `jarvis`
2. In the conversation agent's options, **do not enable an LLM API / "control Home Assistant"**.
   JARVIS controls HA through its own MCP link (step 2) with the approval gate; HA-side tools would
   bypass it and are ignored by the hub anyway.
3. The satellite token can't approve anything: a voice request for a `write` action raises an approval
   card in the HUD/phone and waits up to 90 s. Read-only questions answer directly.

## 4. Wyoming STT/TTS for satellites

Settings → Devices & services → **Add integration** → *Wyoming Protocol*, once per service:

| Service | Host | Port | Needs |
|---|---|---|---|
| Whisper (STT) | `whisper` | `10300` | `--profile voice` |
| Piper (TTS, CPU fallback) | `piper` | `10200` | `--profile voice` |
| JARVIS voice (TTS) | `tts-jarvis` | `10210` | `--profile voicestudio` and the `jarvis` profile ([voice/README.md](../voice/README.md)) |

## 5. The Assist pipeline

Settings → Voice assistants → **Add assistant**:

- Name: `JARVIS`
- Conversation agent: the *Local OpenAI LLM* entry from step 3
- Speech-to-text: `faster-whisper` (Whisper)
- Text-to-speech: `JARVIS` voice (tts-jarvis) or Piper `en_GB-alan-medium`
- Wake word: comes with the ESPHome satellite (Phase 7, microWakeWord `hey_jarvis`)

Test it from the Assist dialog in HA (the chat bubble, or its microphone button).

## Undo

Delete the three integrations in HA, delete the JARVIS user, then
`docker compose -f compose.yaml -f compose.ha.yaml down` (volumes are kept).
