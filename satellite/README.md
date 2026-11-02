# Satellite emulator

A stand-in for an ESPHome voice satellite (Home Assistant Voice PE style), for trying the hub's
satellite path without hardware. It talks to the hub with `HUB_TOKEN_SATELLITE`, so the hub treats
it as `source=satellite`: short spoken answers, and it can never approve an action (those wait for
the HUD or phone).

```
uv run --script satellite/emulator.py                 # wake word + Sendspin output (see below)
uv run --no-project python satellite/emulator.py      # standard library only: push-to-talk, page speaker
```

Open http://127.0.0.1:8792 (localhost, so the browser allows the microphone without HTTPS).

- Say **"Hey Jarvis"** (tick "wake word"), hold the button (or Space), or type what you'd say. The
  wake word is microWakeWord's `hey_jarvis` model, the one a Voice PE runs on the device, through
  `pymicro-wakeword==2.5.0` (OHF, TensorFlow Lite bundled; fetched into uv's cache by `uv run --script`).
  The page streams 16 kHz audio to the emulator in 100 ms blocks; nothing is stored. Recording stops
  after 0.9 s of quiet. `SATELLITE_WAKE_WORD=off` (or okay_nabu, hey_mycroft, alexa) in `.env`.
- Barge-in: the wake word or the button while it's thinking or talking stops it and listens.
- Earcons: rising tones = listening, falling = heard you, a soft pulse while thinking, a tick when a
  tool starts (plus "One moment, sir." if nothing has been said yet), two low tones = something failed.
- When JARVIS asks a question it keeps listening for about 5 s, no wake word needed ("Shall I go ahead,
  sir?" → "yes").
- Announcements from the hub (timers set here, skill results, incoming calls, `ask_claude` answers that
  ran in the background) arrive over the hub's event stream and are spoken when the satellite is idle.
- Failures are spoken: "I can't reach my hub", "My hearing is offline" (Whisper down); if the hub's
  text-to-speech fails, the browser's own voice reads the reply.
- The LED ring follows the ESPHome states: listening (violet, follows your voice), thinking
  (cyan chase), replying (green, follows the audio), error (red blink).
- Replies stream: each sentence is sent to TTS as soon as it's complete; the log shows when the
  first words arrived and how long each sentence took to synthesise.
- Output: the page's own speaker, or Sendspin players (below).

## Sendspin output (e.g. a Bluetooth speaker on another PC)

Run with `uv run --script`: uv reads the inline metadata at the top of `emulator.py` and fetches
`aiosendspin[server]==10.2.2` into its own cache (nothing is installed globally). The emulator then
also runs a Sendspin server on `ws://0.0.0.0:8927/sendspin` (all interfaces, so other PCs can join;
it advertises itself over mDNS as `_sendspin-server._tcp`). If aiosendspin can't be loaded or the
port is taken, the emulator runs exactly as before and the page keeps its own speaker.

On the PC with the speaker:

1. Pair the Bluetooth speaker in that OS and make it the default output device
   (or pick it later with `--audio-device`; list them with `uvx sendspin audio-devices list`).
2. Run a player pointed at this laptop (needs [uv](https://docs.astral.sh/uv/)):
   ```
   uvx sendspin --url ws://<this-laptop-ip>:8927/sendspin
   ```
   (`uvx sendspin daemon --url ...` is the same without the terminal UI.) Leaving out `--url`
   finds the server over mDNS when both machines are on the same network.

In the page, Output → "Sendspin player (N connected: names)" becomes selectable once the server is
up. Each sentence's TTS WAV is then sent to `POST /api/sendspin/play` and queued, back to back, on
every connected player instead of playing in the page; the ring stays green ("replying") for the
estimated length of the audio. Pressing talk again (barge-in) calls `POST /api/sendspin/stop`.
`GET /api/sendspin/status` lists the connected players.

Notes:
- Windows Firewall: the first `uv run --script` may pop up "allow Python to accept connections"; allow it
  on private networks, or players on other PCs can't reach port 8927 (the page and a player on this
  same laptop work either way).
- No pairing: any player that reaches the port is approved for playback (unpaired access), so
  anyone on the LAN can listen in. Fine at home; set `SATELLITE_SENDSPIN=false` elsewhere.
- The emulator does not dial players it discovers on the LAN (so it never takes over speakers that
  Home Assistant / Music Assistant use); players join it. `SATELLITE_SENDSPIN_DISCOVER=true` turns
  dialing on.
- The server key is kept in `data/satellite/sendspin_identity.key` so players see the same server
  across restarts.

Settings (`.env`, optional): `SATELLITE_WAKE_WORD` (hey_jarvis), `SATELLITE_HUB_URL` (default http://127.0.0.1:8700),
`SATELLITE_EMULATOR_PORT` (8792), `SATELLITE_NAME`, `SATELLITE_SENDSPIN` (true),
`SATELLITE_SENDSPIN_PORT` (8927), `SATELLITE_SENDSPIN_HOST` (0.0.0.0),
`SATELLITE_SENDSPIN_DISCOVER` (false).
