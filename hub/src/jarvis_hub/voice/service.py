"""Speech for the web/phone path: STT over Wyoming (faster-whisper) and TTS via
VoiceStudio (cloned JARVIS voice) with Piper over Wyoming as the CPU fallback.
Satellites don't use this: Home Assistant runs their STT/TTS itself."""

import io
import logging
import wave
from urllib.parse import urlparse

import httpx
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize

log = logging.getLogger(__name__)
CHUNK_BYTES = 4096


class VoiceError(RuntimeError):
    pass


def _host_port(uri: str) -> tuple[str, int]:
    u = urlparse(uri)
    if u.scheme != "tcp" or not u.hostname or not u.port:
        raise ValueError(f"expected tcp://host:port, got {uri!r}")
    return u.hostname, u.port


class VoiceService:
    def __init__(self, *, whisper_uri: str, piper_uri: str, engine: str,
                 voicestudio_url: str, voicestudio_key: str, voicestudio_model: str,
                 voicestudio_voice: str, language: str = "en") -> None:
        self.whisper_uri, self.piper_uri, self.engine = whisper_uri, piper_uri, engine
        self.language = language
        self._vs_model, self._vs_voice = voicestudio_model, voicestudio_voice
        self._vs = httpx.AsyncClient(
            base_url=voicestudio_url.rstrip("/"), timeout=httpx.Timeout(120, connect=3),
            headers={"Authorization": f"Bearer {voicestudio_key}"} if voicestudio_key else {})

    # ---------- STT ----------
    async def transcribe(self, wav_bytes: bytes) -> str:
        try:
            with wave.open(io.BytesIO(wav_bytes)) as w:
                rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
                pcm = w.readframes(w.getnframes())
        except (wave.Error, EOFError) as e:
            raise VoiceError(f"expected a PCM WAV upload: {e}") from e

        host, port = _host_port(self.whisper_uri)
        try:
            async with AsyncTcpClient(host, port) as client:
                await client.write_event(Transcribe(language=self.language).event())
                await client.write_event(AudioStart(rate, width, channels).event())
                for i in range(0, len(pcm), CHUNK_BYTES):
                    await client.write_event(
                        AudioChunk(rate, width, channels, pcm[i:i + CHUNK_BYTES]).event())
                await client.write_event(AudioStop().event())
                while True:
                    event = await client.read_event()
                    if event is None:
                        raise VoiceError("whisper closed the connection")
                    if Transcript.is_type(event.type):
                        return Transcript.from_event(event).text.strip()
        except OSError as e:
            raise VoiceError(f"whisper unreachable at {self.whisper_uri}: {e}") from e

    # ---------- TTS ----------
    async def speak(self, text: str) -> tuple[bytes, str]:
        """Return (wav_bytes, engine_used)."""
        if self.engine in ("auto", "voicestudio"):
            try:
                return await self._voicestudio(text), "voicestudio"
            except (httpx.HTTPError, VoiceError) as e:
                if self.engine == "voicestudio":
                    raise VoiceError(f"VoiceStudio failed: {e}") from e
                log.info("VoiceStudio unavailable, falling back to Piper: %s", e)
        return await self._piper(text), "piper"

    async def _voicestudio(self, text: str) -> bytes:
        r = await self._vs.post("/audio/speech", json={
            "model": self._vs_model, "voice": self._vs_voice, "input": text,
            "response_format": "wav"})
        if r.status_code != 200:
            raise VoiceError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.content

    async def _piper(self, text: str) -> bytes:
        host, port = _host_port(self.piper_uri)
        buf = io.BytesIO()
        writer: wave.Wave_write | None = None
        try:
            async with AsyncTcpClient(host, port) as client:
                await client.write_event(Synthesize(text=text).event())
                while True:
                    event = await client.read_event()
                    if event is None:
                        break
                    if AudioStart.is_type(event.type):
                        start = AudioStart.from_event(event)
                        writer = wave.open(buf, "wb")
                        writer.setframerate(start.rate)
                        writer.setsampwidth(start.width)
                        writer.setnchannels(start.channels)
                    elif AudioChunk.is_type(event.type) and writer:
                        writer.writeframes(AudioChunk.from_event(event).audio)
                    elif AudioStop.is_type(event.type):
                        break
        except OSError as e:
            raise VoiceError(f"piper unreachable at {self.piper_uri}: {e}") from e
        if writer is None:
            raise VoiceError("piper returned no audio")
        writer.close()
        return buf.getvalue()

    async def status(self) -> dict:
        out = {"engine": self.engine}
        try:
            r = await self._vs.get("/models", timeout=3)
            out["voicestudio"] = "up" if r.status_code == 200 else f"http {r.status_code}"
        except httpx.HTTPError:
            out["voicestudio"] = "down"
        return out

    async def aclose(self) -> None:
        await self._vs.aclose()
