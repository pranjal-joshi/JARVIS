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
   Accept the OmniVoice licence in **Model Catalogue** (CC-BY-NC weights: non-commercial use only).
   Speech returns 403 `model_licence_required` until you do; the hub falls back to Piper meanwhile.
   Clips recorded on a phone/PC (m4a etc.) can be converted with the ffmpeg in the image:
   `docker run --rm --entrypoint ffmpeg -v "$PWD/voice/reference:/ref" ghcr.io/debpalash/voicestudio:0.5.7 -i /ref/in.m4a -ac 1 -ar 24000 /ref/jarvis.wav`
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

## JARVIS Piper voice (fast, no GPU)

[jgkawell/jarvis](https://huggingface.co/jgkawell/jarvis) (MIT) is a Piper voice fine-tuned from `en_GB-alan-medium`.
Piper reads custom voices from `voice/piper/` (mounted read-only; the files are git-ignored):

```bash
B=https://huggingface.co/jgkawell/jarvis/resolve/37f8763122312665f091d1fc760abaf1f79b02cc/en/en_GB/jarvis
for q in medium high; do curl -L -o voice/piper/jarvis-$q.onnx $B/$q/jarvis-$q.onnx; curl -L -o voice/piper/jarvis-$q.onnx.json $B/$q/jarvis-$q.onnx.json; done
```

Then set `PIPER_VOICE=jarvis-medium` in `.env` and `docker compose up -d piper`.
Measured on this laptop (CPU), one two-sentence line: `jarvis-medium` ~0.6 s, `jarvis-high` ~3.5 s,
Kokoro `bm_george` ~3.7 s (tried and removed: too slow on CPU).

### Jarvis_Real (alternative)

[gstratto75/Jarvis_Real](https://github.com/gstratto75/Jarvis_Real) is another Piper medium voice (US espeak,
22.05 kHz, speaks ~25% slower via `length_scale` 1.25). The repo has **no licence**: personal use only.

```bash
B=https://github.com/gstratto75/Jarvis_Real/raw/c5cce6f21b7295f67e431c16057df2df1adc057a
for f in en_US-Jarvis_Real-medium.onnx en_US-Jarvis_Real-medium.onnx.json; do curl -L -o voice/piper/$f $B/$f; done
```

Then `PIPER_VOICE=en_US-Jarvis_Real-medium`. Measured ~0.5 s for a two-sentence line once loaded.
Stock voice: `PIPER_VOICE=en_GB-alan-medium` (the default).
