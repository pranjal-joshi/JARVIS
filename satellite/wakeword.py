"""Wake word for the satellite emulator: "Hey Jarvis", with the same microWakeWord model an ESPHome
satellite (Home Assistant Voice PE) runs on the device. The page streams its microphone here as
16 kHz mono 16-bit PCM in short blocks; nothing is stored or sent anywhere else.

Needs pymicro-wakeword (TensorFlow Lite, bundled), which `uv run --script satellite/emulator.py`
fetches into uv's cache from the emulator's inline metadata. Without it the emulator still runs,
push-to-talk only."""

import logging
import threading

log = logging.getLogger("satellite.wake")
BYTES_10MS = 320  # 160 samples x 2 bytes


class WakeWord:
    def __init__(self, model: str = "hey_jarvis") -> None:
        from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model

        self.name = model
        self._mww = MicroWakeWord.from_builtin(Model(model))
        self._features = MicroWakeWordFeatures()
        self._buf = b""
        self._lock = threading.Lock()

    def feed(self, pcm: bytes) -> bool:
        """True when the wake word ends somewhere in this audio (the detector then starts afresh)."""
        with self._lock:
            self._buf += pcm
            hit = False
            while len(self._buf) >= BYTES_10MS:
                chunk, self._buf = self._buf[:BYTES_10MS], self._buf[BYTES_10MS:]
                for features in self._features.process_streaming(chunk):
                    if self._mww.process_streaming(features):
                        hit = True
            if hit:
                self._reset()
            return hit

    def reset(self) -> None:
        with self._lock:
            self._reset()

    def _reset(self) -> None:
        self._mww.reset()
        self._features.reset()
        self._buf = b""


def load(model: str) -> WakeWord | None:
    try:
        return WakeWord(model)
    except ImportError as e:
        log.info("wake word off (%s); run with `uv run --script` to enable it", e)
    except Exception as e:  # missing model, TFLite load failure: push-to-talk still works
        log.warning("wake word off: %r", e)
    return None
