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

## 6. Voice-only use (satellite with no screen)

The hub already handles these for any client using `HUB_TOKEN_SATELLITE`; the steps below are the HA
side. Nothing here changes the approval rules.

| What | Hub side (done) | HA side (you) |
|---|---|---|
| Wake word | — | ESPHome satellite → wake word **hey_jarvis** (microWakeWord, on the device). Voice PE: device page → *Wake word* select. |
| Interrupt by voice | — | Voice PE stops playback when the wake word is heard again; leave *Stop word* on. |
| Follow-ups ("yes, send it") | Keeps each satellite's last turns for `VOICE_FOLLOWUP_S` (120 s), used when HA sends one turn at a time | HA 2025.4+ keeps the mic open when the reply ends in "?" (continue conversation). Nothing to set. |
| Saying "yes" to web search / ask_claude | `VOICE_CONFIRM_RISKS=external`: the satellite asks "Shall I go ahead, sir?" and acts on your next words | — |
| Texts, calls, deletes (`destructive`) | Satellite says "Please approve on your phone or the HUD: send a text to Chiku saying …" | Optional push: set `HA_NOTIFY_SERVICE=mobile_app_<your phone>` and `HUD_URL=https://<laptop-ip>:3443`; tapping the notification opens the HUD. |
| Timers, skill results, incoming calls, background ask_claude answers | Published as `announce` events | Set `HA_ANNOUNCE_ENTITY=assist_satellite.<your satellite>`; the hub calls `assist_satellite.announce`. |
| Thinking sound during long tool calls | Streams tool-start cues (emulator only) | Voice PE: enable the *processing sound* on the device page. |
| Times, dates, numbers said naturally | Non-streamed satellite replies and satellite TTS go through `for_ear` | — |
| Misheard names | Names from KB page titles, the page `jarvis/vocabulary` (one name per block) and recent contacts are put right after Whisper | — |

The `HA_*` calls use `HA_TOKEN`. If that token's user is not an admin, give it access to the notify
and assist_satellite services. With these unset, nothing is sent to HA.

## Undo

Delete the three integrations in HA, delete the JARVIS user, then
`docker compose -f compose.yaml -f compose.ha.yaml down` (volumes are kept).
