# Voice

| Path | STT | TTS |
|---|---|---|
| HA satellites | `whisper` (Wyoming, faster-whisper) | `tts-jarvis` (cloned voice) or `piper` |
| Browser HUD | hub → `whisper` | hub → VoiceStudio, falling back to `piper` |

`TTS_ENGINE=auto` (default) uses VoiceStudio when it answers and Piper otherwise, so voice
keeps working before the cloned voice exists, or if VoiceStudio is stopped.

## Setting up the cloned JARVIS voice (VoiceStudio)

[debpalash/VoiceStudio](https://github.com/debpalash/VoiceStudio) v0.5.7, AGPL-3.0. Only use the
official image `ghcr.io/debpalash/voicestudio`; GitHub has look-alike mirror repos under unrelated accounts.

1. Put a clean reference clip in `voice/reference/` (WAV or MP3, 10–30 s, one speaker,
   no music or effects). This folder is git-ignored and mounted read-only into the container.
2. Start it (first start downloads ~2.4 GB of weights):
   ```bash
   docker compose --profile voice --profile voicestudio up -d
   ```
3. Open http://localhost:3900 and enter `VOICESTUDIO_API_KEY` from `.env` when asked.
4. **Voice cloning** → add the reference clip (it is at `/reference/<file>` inside the container,
   or upload it from your disk) → save the profile with the name **`jarvis`**
   (must match `VOICESTUDIO_VOICE` in `.env`).
5. Check it: in the HUD the `tts` chip switches from `piper` to `jarvis`. Or:
   ```bash
   curl -s -X POST localhost:3900/v1/audio/speech -H 'Content-Type: application/json' \
     -d '{"model":"tts-1","voice":"jarvis","input":"Good evening, sir.","response_format":"wav"}' -o test.wav
   ```

Keep it private: the profile and clip stay in the `voicestudio-data` volume and `voice/reference/`,
neither is committed, and nothing is reachable from outside the machine.

### CPU performance

On this laptop (i7-1260P, no usable GPU) VoiceStudio is slower than real time for long replies.
The persona keeps voice replies to two sentences. If it is still too slow, set `TTS_ENGINE=piper`
until the homelab GPU is available (`compose.gpu.yaml`).

### Alternative

wyoming-piper 2.5.2 ships an `omnivoice` backend (image tag `2.5.2-omnivoice`) that runs the same
OmniVoice model directly over Wyoming from a folder of reference clips. It is not wired up; it's an
option if you want HA to use the cloned voice without VoiceStudio running.
