"""Satellite emulator: a stand-in for an ESPHome voice satellite (e.g. Home Assistant Voice PE),
so the hub's satellite path can be exercised without hardware.

It speaks to the hub exactly as a satellite would, with the satellite token, so the hub treats
it as source=satellite: short spoken answers, and it can never approve an action.

Runs on the host, bound to 127.0.0.1 (browsers allow the microphone on localhost without HTTPS).
Standard library only:  uv run --no-project python satellite/emulator.py   ->  http://127.0.0.1:8792
"""

import json
import logging
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
log = logging.getLogger("satellite")


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith(("SATELLITE_", "HUB_"))}}


CFG = load_env(ROOT / ".env")
HUB = CFG.get("SATELLITE_HUB_URL", "http://127.0.0.1:8700").rstrip("/")
TOKEN = CFG.get("HUB_TOKEN_SATELLITE", "")
PORT = int(CFG.get("SATELLITE_EMULATOR_PORT", "8792"))
NAME = CFG.get("SATELLITE_NAME", "Satellite (emulated)")
MAX_BODY = 10 * 1024 * 1024


def hub(path: str, data: bytes | None, ctype: str | None, timeout: float = 200):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if ctype:
        headers["Content-Type"] = ctype
    return urlopen(Request(f"{HUB}/v1/{path}", data=data, headers=headers,
                           method="POST" if data is not None else "GET"), timeout=timeout)


class Handler(BaseHTTPRequestHandler):
    server_version = "jarvis-satellite-emulator"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/info":
            return self._json(200, {"name": NAME, "hub": HUB, "token": bool(TOKEN)})
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            return self._json(413, {"detail": "too large"})
        body = self.rfile.read(length)
        try:
            if self.path == "/api/stt":  # raw 16 kHz WAV from the page -> multipart for the hub
                b = uuid.uuid4().hex
                form = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                        f"Content-Type: audio/wav\r\n\r\n").encode() + body + f"\r\n--{b}--\r\n".encode()
                with hub("audio/transcriptions", form, f"multipart/form-data; boundary={b}") as r:
                    return self._send(200, r.read(), "application/json")
            if self.path == "/api/tts":
                with hub("audio/speech", body, "application/json", 60) as r:
                    return self._send(r.status, r.read(), r.headers.get("Content-Type", "audio/wav"))
            if self.path == "/api/chat":  # stream the hub's SSE straight through
                req = json.loads(body or b"{}")
                payload = json.dumps({"model": "jarvis", "stream": True,
                                      "messages": req.get("messages") or []}).encode()
                with hub("chat/completions", payload, "application/json") as r:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while chunk := r.read1(4096):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                return
            self._json(404, {"detail": "not found"})
        except HTTPError as e:
            self._json(e.code, {"detail": e.read().decode("utf-8", "replace")[:300]})
        except (URLError, TimeoutError) as e:
            self._json(502, {"detail": f"hub unreachable: {e}"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN or TOKEN == "change-me":
        raise SystemExit("set HUB_TOKEN_SATELLITE in .env first")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("satellite emulator on http://127.0.0.1:%d -> hub %s", PORT, HUB)
    server.serve_forever()


if __name__ == "__main__":
    main()
