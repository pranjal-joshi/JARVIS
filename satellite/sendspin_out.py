"""Sendspin output for the satellite emulator: speak replies on Sendspin players instead of the page.

A small Sendspin server (aiosendspin) that players join, e.g. `uvx sendspin --url ws://HOST:8927/sendspin`
on a PC with a Bluetooth speaker. Each sentence's TTS WAV is decoded and pushed to every connected
player's group; sentences queue back to back on the same stream.

The emulator's HTTP server is threaded stdlib, so the asyncio server runs in its own thread with its
own loop; the HTTP handlers hand audio over with asyncio.run_coroutine_threadsafe (see SendspinOut.call).

API used (aiosendspin 10.2.2):
  server/server.py       SendspinServer(loop, identity, name, *, pairing_store, allow_unencrypted)
                         .start_server(port, host, discover_clients=...), .connected_clients, .close()
  noise/keys.py          Identity.generate() / .from_private_bytes() / .private_bytes
  noise/trust_store.py   InMemoryServerPairingStore()  (no pairing; players allow unpaired access)
                         server.trust_unpaired(client_id) on ClientConnectedEvent: an encrypted but
                         unpaired player only gets its player role once the server approves it
  server/client.py       client.roles_by_family("player"), client.group, client.name
  server/group.py        group.start_stream() -> PushStream, group.stop_stream()
  server/push_stream.py  ps.prepare_audio(pcm, AudioFormat), await ps.commit_audio(),
                         await ps.sleep_to_limit_buffer(0) (drain), ps.clear()
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave
from pathlib import Path

from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import AudioFormat, ClientConnectedEvent, SendspinServer

log = logging.getLogger("satellite.sendspin")

SLICE_S = 0.1  # commit PCM in 100 ms slices, like Music Assistant's producer
IDLE_STOP_S = 5.0  # end the stream (players release their audio device) this long after the last audio


class SendspinOut:
    def __init__(self, name: str, port: int = 8927, host: str = "0.0.0.0",
                 identity_file: Path | None = None, discover: bool = False) -> None:
        self.name, self.port, self.host, self.discover = name, port, host, discover
        self.identity_file = identity_file
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: SendspinServer | None = None
        self.error: str | None = None
        self._streams: dict = {}  # SendspinGroup -> its PushStream (one per group, reused across sentences)
        self._lock: asyncio.Lock | None = None  # keeps sentences in order
        self._idle: asyncio.Task | None = None

    # ---- lifecycle: own thread, own loop ----
    def start(self, timeout: float = 20) -> bool:
        ready = threading.Event()

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._start())
            except Exception as e:  # port in use, etc.: the emulator carries on without Sendspin
                self.error = f"{type(e).__name__}: {e}"
                log.warning("sendspin server failed to start: %s", self.error)
                ready.set()
                return
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, name="sendspin", daemon=True).start()
        ready.wait(timeout)
        return self.server is not None and self.error is None

    def _identity(self) -> Identity:
        # a stable server id across restarts, so players see the same server each time
        f = self.identity_file
        if f and f.exists():
            return Identity.from_private_bytes(f.read_bytes())
        ident = Identity.generate()
        if f:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(ident.private_bytes)
        return ident

    async def _start(self) -> None:
        self._lock = asyncio.Lock()
        server = SendspinServer(asyncio.get_running_loop(), self._identity(), self.name,
                                pairing_store=InMemoryServerPairingStore(), allow_unencrypted=True)
        server.add_event_listener(self._on_event)
        # discover_clients=False: don't dial every Sendspin player advertised on the LAN (e.g. ones
        # Home Assistant / Music Assistant own); players join us by URL or by finding our mDNS ad.
        await server.start_server(port=self.port, host=self.host, discover_clients=self.discover)
        self.server = server
        log.info("sendspin server on ws://%s:%d/sendspin", self.host, self.port)

    def _on_event(self, server: SendspinServer, event) -> None:
        # Approve every player that joins for unpaired playback (the emulator's simplification: anyone
        # on the LAN who can reach the port may listen). Without it the player connects but gets no role.
        if isinstance(event, ClientConnectedEvent):
            log.info("sendspin player connected: %s", event.client_id[:12])
            asyncio.create_task(server.trust_unpaired(event.client_id))

    def call(self, coro, timeout: float = 30):
        """Run a coroutine on the Sendspin loop from an HTTP handler thread and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        if self.server and self.loop:
            try:
                self.call(self.server.close(), 5)
            except Exception:
                pass

    # ---- players ----
    def players(self) -> list:
        if not self.server:
            return []
        # connected, and its player role active (i.e. approved, see _on_event)
        return [c for c in self.server.connected_clients if c.roles_by_family("player")]

    async def status(self) -> dict:
        return {"enabled": True, "running": self.server is not None, "port": self.port,
                "players": [c.name for c in self.players()], "error": self.error}

    # ---- audio ----
    async def play_wav(self, wav_bytes: bytes) -> dict:
        """Queue one sentence on every connected player; returns once committed (not once played)."""
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            pcm = w.readframes(w.getnframes())
        if width not in (2, 3, 4):
            raise ValueError(f"unsupported WAV sample width {width * 8} bit")
        fmt = AudioFormat(sample_rate=rate, bit_depth=width * 8, channels=ch)
        duration = len(pcm) / (rate * ch * width)
        players = self.players()
        groups = {id(c.group): c.group for c in players}.values()  # grouped players share one stream
        step = int(rate * SLICE_S) * ch * width
        async with self._lock:
            if self._idle:
                self._idle.cancel()
            for group in groups:
                ps = self._streams.get(group)
                if ps is None or ps.is_stopped:
                    ps = self._streams[group] = group.start_stream()
                # slices keep queuing back to back on the stream's timeline, after the previous sentence
                for off in range(0, len(pcm), step):
                    ps.prepare_audio(pcm[off:off + step], fmt)
                    await ps.commit_audio()
            self._idle = asyncio.create_task(self._stop_when_idle())
        return {"players": len(players), "duration_s": round(duration, 3)}

    async def _stop_when_idle(self) -> None:
        # wait for the players to play out what was queued, then end the stream (stream/end would
        # otherwise cut off audio still in the players' buffers); a new sentence cancels this
        for group, ps in list(self._streams.items()):
            while not ps.is_stopped:
                t0 = asyncio.get_running_loop().time()
                await ps.sleep_to_limit_buffer(0)
                if asyncio.get_running_loop().time() - t0 < 0.05:
                    break
        await asyncio.sleep(IDLE_STOP_S)
        self._end_streams()

    async def stop(self) -> None:
        """Barge-in: drop whatever is still queued on the players."""
        if self._idle:
            self._idle.cancel()
        for ps in self._streams.values():
            if not ps.is_stopped:
                ps.clear()
        self._end_streams()

    def _end_streams(self) -> None:
        for group, ps in list(self._streams.items()):
            if not ps.is_stopped:
                group.stop_stream()
        self._streams.clear()
