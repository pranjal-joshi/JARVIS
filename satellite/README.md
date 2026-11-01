# Satellite emulator

A stand-in for an ESPHome voice satellite (Home Assistant Voice PE style), for trying the hub's
satellite path without hardware. It talks to the hub with `HUB_TOKEN_SATELLITE`, so the hub treats
it as `source=satellite`: short spoken answers, and it can never approve an action (those wait for
the HUD or phone).

```
uv run --no-project python satellite/emulator.py
```

Open http://127.0.0.1:8792 (localhost, so the browser allows the microphone without HTTPS).

- Hold the button (or Space) to talk, or type what you'd say.
- The LED ring follows the ESPHome states: listening (violet, follows your voice), thinking
  (cyan chase), replying (green, follows the audio), error (red blink).
- Replies stream: each sentence is sent to TTS as soon as it's complete; the log shows when the
  first words arrived and how long each sentence took to synthesise.
- Output: the page's own speaker. A Sendspin output (to a Bluetooth speaker via a `sendspin`
  player) is listed but not wired up yet.

Settings (`.env`, optional): `SATELLITE_HUB_URL` (default http://127.0.0.1:8700),
`SATELLITE_EMULATOR_PORT` (8792), `SATELLITE_NAME`.
