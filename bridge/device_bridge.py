"""Device bridge: lets the JARVIS hub (in Docker) act on this Windows laptop and on Android
devices reached over LAN ADB (wireless debugging).

Runs on the host, bound to 127.0.0.1 (containers reach it as host.docker.internal).
Standard library only:  uv run --no-project python bridge/device_bridge.py

Safety:
- bearer token (DEVICE_BRIDGE_TOKEN from .env), constant-time compare;
- a fixed list of actions, no shell passthrough on Windows; apps open only from DEVICE_WIN_APPS;
- Android: only devices whose hardware serial (ro.serialno) is in DEVICE_ADB_ALLOW are touched
  (the wireless-debugging ip:port changes every session, the hardware serial doesn't);
- the hub decides which actions need approval (tap, type, install, uninstall, shell);
  this bridge only enforces the allow-lists; logs record the action name, never the arguments.
"""

import ctypes
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from android_comms import Comms

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("device-bridge")


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith(("DEVICE_", "ADB_"))}}


CFG = load_env(ROOT / ".env")
TOKEN = CFG.get("DEVICE_BRIDGE_TOKEN", "")
PORT = int(CFG.get("DEVICE_BRIDGE_PORT", "8791"))
ADB = CFG.get("ADB_PATH", "adb")
ADB_ALLOW = {s.strip() for s in CFG.get("DEVICE_ADB_ALLOW", "").split(",") if s.strip()}
# devices allowed from the HUD's Devices panel (after QR pairing) are kept here, next to the .env list
ALLOW_FILE = ROOT / "data" / "adb-allow.json"


def _allowed() -> set[str]:
    try:
        return ADB_ALLOW | set(json.loads(ALLOW_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set(ADB_ALLOW)
# name=target pairs: an exe/shell target (e.g. notepad, calc, spotify:) or a .lnk/.exe path
WIN_APPS = dict(p.split("=", 1) for p in CFG.get("DEVICE_WIN_APPS", "notepad=notepad.exe,calculator=calc.exe")
                .split(",") if "=" in p)
SHOTS = ROOT / "data" / "screenshots"
TIMEOUT = 20


class ActionError(RuntimeError):
    pass


# ---------------------------------------------------------------- Windows
user32 = ctypes.windll.user32 if os.name == "nt" else None
VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
      "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF}


class _Power(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


class _Mem(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
               [(n, ctypes.c_ulonglong) for n in ("total", "avail", "pt", "pa", "vt", "va", "ve")]


def win_info(_: dict) -> str:
    p = _Power()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p))
    m = _Mem()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
    up = ctypes.windll.kernel32.GetTickCount64() // 1000
    battery = "unknown" if p.BatteryLifePercent in (-1, 255) else f"{p.BatteryLifePercent & 0xFF}%"
    return (f"Battery {battery}, {'charging' if p.ACLineStatus == 1 else 'on battery'}; "
            f"memory {m.dwMemoryLoad}% used of {m.total / 2**30:.1f} GB; "
            f"up {up // 3600} h {up % 3600 // 60} min; active window: {buf.value or '(none)'}")


def win_media(a: dict) -> str:
    key = str(a.get("key", ""))
    if key not in VK:
        raise ActionError(f"key must be one of {sorted(VK)}")
    times = max(1, min(int(a.get("times") or 1), 25)) if key.startswith("volume") else 1
    for _ in range(times):
        user32.keybd_event(VK[key], 0, 0, 0)
        user32.keybd_event(VK[key], 0, 2, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.03)
    return f"Pressed {key}" + (f" x{times}" if times > 1 else "") + "."


def win_open(a: dict) -> str:
    target = str(a.get("target", "")).strip()
    if re.match(r"^https?://", target, re.I):
        webbrowser.open(target)
        return f"Opened {target} in the browser."
    app = WIN_APPS.get(target.lower())
    if not app:
        raise ActionError(f"not an allowed app; allowed: {', '.join(sorted(WIN_APPS)) or 'none'}")
    os.startfile(app)  # allow-listed target only, no arguments
    return f"Opened {target}."


def win_lock(_: dict) -> str:
    if not user32.LockWorkStation():
        raise ActionError("lock failed")
    return "Workstation locked."


def win_screenshot(_: dict) -> str:
    SHOTS.mkdir(parents=True, exist_ok=True)
    out = SHOTS / f"win-{time.strftime('%Y%m%d-%H%M%S')}.png"
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
          "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
          "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
          "$g=[System.Drawing.Graphics]::FromImage($i);$g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size);"
          f"$i.Save('{out}');")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=TIMEOUT, check=True)
    return f"Screenshot saved to {out.relative_to(ROOT)}."


# ---------------------------------------------------------------- Android (ADB)
def adb(*args: str, serial: str | None = None, binary: bool = False, timeout: int = TIMEOUT):
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise ActionError((p.stderr or p.stdout).decode("utf-8", "replace").strip()[-300:] or "adb failed")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace").strip()


def _devices() -> list[dict]:
    out = []
    for line in adb("devices", "-l").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        d = {"id": parts[0], "state": parts[1],
             "model": next((p[6:] for p in parts if p.startswith("model:")), "")}
        if d["state"] == "device":
            try:
                d["serial"] = adb("shell", "getprop", "ro.serialno", serial=d["id"])
            except ActionError:
                d["serial"] = ""
        d["allowed"] = d.get("serial", "") in _allowed()
        out.append(d)
    return out


def _device(a: dict) -> str:
    """The adb id of an allowed device: the one named (model, serial or id), or the only one."""
    want = str(a.get("device") or "").strip().lower()
    ok = [d for d in _devices() if d["allowed"]]
    if want:
        ok = [d for d in ok if want in (d["id"].lower(), d.get("serial", "").lower()) or want in d["model"].lower()]
    if not ok:
        raise ActionError("no allowed Android device connected (see adb_devices; add its serial to DEVICE_ADB_ALLOW)")
    if len(ok) > 1:
        raise ActionError("several allowed devices: say which one (" + ", ".join(d["model"] or d["id"] for d in ok) + ")")
    return ok[0]["id"]


def adb_devices(_: dict) -> str:
    ds = _devices()
    if not ds:
        return "No Android devices connected over ADB."
    return "\n".join(f"{d['model'] or '?'} id={d['id']} serial={d.get('serial', '?')} state={d['state']} "
                     f"{'allowed' if d['allowed'] else 'NOT allowed'}" for d in ds)


def adb_info(a: dict) -> str:
    s = _device(a)
    bat = adb("shell", "dumpsys", "battery", serial=s)
    level = re.search(r"level: (\d+)", bat)
    plugged = re.search(r"(AC|USB|Wireless) powered: true", bat)
    focus = adb("shell", "dumpsys", "window", serial=s)
    app = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}/ ]+)", focus)
    model = adb("shell", "getprop", "ro.product.model", serial=s)
    ver = adb("shell", "getprop", "ro.build.version.release", serial=s)
    return (f"{model} (Android {ver}): battery {level[1] if level else '?'}%"
            f"{', charging' if plugged else ''}; foreground app: {app[1] if app else 'unknown'}")


def adb_screenshot(a: dict) -> str:
    s = _device(a)
    SHOTS.mkdir(parents=True, exist_ok=True)
    out = SHOTS / f"android-{time.strftime('%Y%m%d-%H%M%S')}.png"
    out.write_bytes(adb("exec-out", "screencap", "-p", serial=s, binary=True))
    return f"Screenshot saved to {out.relative_to(ROOT)}."


PKG = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z][\w]*)+$")


def adb_open_app(a: dict) -> str:
    s = _device(a)
    pkg = str(a.get("package", "")).strip()
    if not PKG.match(pkg):
        name = pkg.lower()
        pkgs = [l.removeprefix("package:") for l in adb("shell", "pm", "list", "packages", serial=s).splitlines()]
        hits = [p for p in pkgs if name and name.replace(" ", "") in p.lower()]
        if len(hits) != 1:
            raise ActionError(f"give the package name; matches: {', '.join(hits[:8]) or 'none'}")
        pkg = hits[0]
    adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1", serial=s)
    return f"Opened {pkg}."


KEYS = {"play_pause": 85, "next": 87, "previous": 88, "volume_up": 24, "volume_down": 25, "mute": 164,
        "home": 3, "back": 4, "wake": 224, "sleep": 223}


def adb_key(a: dict) -> str:
    s = _device(a)
    key = str(a.get("key", ""))
    if key not in KEYS:
        raise ActionError(f"key must be one of {sorted(KEYS)}")
    adb("shell", "input", "keyevent", str(KEYS[key]), serial=s)
    return f"Sent {key}."


def adb_tap(a: dict) -> str:
    s = _device(a)
    x, y = int(a.get("x")), int(a.get("y"))
    adb("shell", "input", "tap", str(x), str(y), serial=s)
    return f"Tapped {x},{y}."


def adb_type(a: dict) -> str:
    s = _device(a)
    text = str(a.get("text", ""))[:500]
    # input text needs spaces as %s and shell metacharacters escaped
    esc = re.sub(r"([\\\"'`$&|;<>()*?#~ ])", lambda m: "%s" if m[1] == " " else "\\" + m[1], text)
    adb("shell", "input", "text", esc, serial=s)
    return f"Typed {len(text)} characters."


def adb_shell(a: dict) -> str:
    s = _device(a)
    cmd = str(a.get("command", "")).strip()
    if not cmd:
        raise ActionError("empty command")
    return adb("shell", cmd, serial=s, timeout=30)[-4000:] or "(no output)"


def adb_install(a: dict) -> str:
    s = _device(a)
    apk = (ROOT / str(a.get("apk", ""))).resolve()
    drop = (ROOT / "data" / "apk").resolve()
    if apk.parent != drop or apk.suffix.lower() != ".apk" or not apk.exists():
        raise ActionError(f"put the .apk in {drop} and give its file name")
    return adb("install", "-r", str(apk), serial=s, timeout=180)[-300:]


def adb_uninstall(a: dict) -> str:
    s = _device(a)
    pkg = str(a.get("package", "")).strip()
    if not PKG.match(pkg):
        raise ActionError("give the exact package name")
    return adb("uninstall", pkg, serial=s, timeout=60)


def adb_connect(a: dict) -> str:
    addr = str(a.get("address", "")).strip()
    if not re.match(r"^(\d{1,3}\.){3}\d{1,3}:\d{2,5}$", addr):
        raise ActionError("address must be ip:port from the phone's Wireless debugging screen")
    return adb("connect", addr)


# ---------------------------------------------------------------- QR pairing (Android 11+)
# The phone scans WIFI:T:ADB;S:<name>;P:<code>;; then advertises <name> as _adb-tls-pairing._tcp
# over mDNS; we find it with `adb mdns services`, pair with the code, and adb then connects to the
# phone's _adb-tls-connect._tcp service on its own. Several phones can be paired, one QR each.
PAIRINGS: dict[str, dict] = {}
PAIR_TIMEOUT = 150


def _pair_worker(job: dict) -> None:
    deadline = time.monotonic() + PAIR_TIMEOUT
    try:
        while time.monotonic() < deadline and job["status"] == "waiting":
            for line in adb("mdns", "services").splitlines():
                if job["name"] in line and "_adb-tls-pairing" in line:
                    addr = line.split()[-1]
                    job["status"], job["address"] = "pairing", addr
                    out = adb("pair", addr, job["code"], timeout=30)
                    if "Successfully paired" not in out:
                        raise ActionError(out[-200:])
                    job["status"] = "connecting"
                    break
            else:
                time.sleep(1)
                continue
            break
        if job["status"] == "waiting":
            job["status"] = "expired"
            return
        before = job["known"]
        while time.monotonic() < deadline:  # wait for adb's own auto-connect to bring the phone up
            new = [d for d in _devices() if d["state"] == "device" and d.get("serial") and d["id"] not in before]
            if new:
                job.update(status="connected", serial=new[0]["serial"], model=new[0]["model"],
                           allowed=new[0]["allowed"])
                return
            time.sleep(1.5)
        job["status"] = "paired"  # paired, but not connected yet: tap the phone's connect entry
    except (ActionError, subprocess.TimeoutExpired) as e:
        job.update(status="failed", error=str(e)[:200])


def adb_pair_start(_: dict) -> dict:
    for k in [k for k, j in PAIRINGS.items() if time.time() - j["created"] > 600]:
        PAIRINGS.pop(k)
    jid = secrets.token_hex(4)
    job = {"id": jid, "name": f"jarvis-{jid}", "code": f"{secrets.randbelow(10**6):06d}", "status": "waiting",
           "created": time.time(), "known": {d["id"] for d in _devices()}}
    PAIRINGS[jid] = job
    threading.Thread(target=_pair_worker, args=(job,), daemon=True).start()
    return {"id": jid, "qr": f"WIFI:T:ADB;S:{job['name']};P:{job['code']};;", "status": "waiting",
            "expires_in": PAIR_TIMEOUT}


def adb_pair_status(a: dict) -> dict:
    job = PAIRINGS.get(str(a.get("id", "")))
    if not job:
        raise ActionError("no such pairing")
    return {k: job.get(k) for k in ("id", "status", "address", "serial", "model", "allowed", "error")}


def adb_list(_: dict) -> list[dict]:
    return _devices()


def adb_allow(a: dict) -> str:
    serial = str(a.get("serial", "")).strip()
    if not re.match(r"^[\w.-]{4,64}$", serial):
        raise ActionError("bad serial")
    if serial in ADB_ALLOW and not a.get("allowed", True):
        raise ActionError("that serial is allowed in .env (DEVICE_ADB_ALLOW); remove it there")
    try:
        cur = set(json.loads(ALLOW_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        cur = set()
    cur = cur | {serial} if a.get("allowed", True) else cur - {serial}
    ALLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALLOW_FILE.write_text(json.dumps(sorted(cur)), encoding="utf-8")
    return f"{serial} {'allowed' if a.get('allowed', True) else 'no longer allowed'}."


# ---------------------------------------------------------------- calls, messages, notifications
comms = Comms(adb, ActionError)


def _when(ts: int) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else "?"


def adb_notifications(a: dict) -> str:
    items = comms.notifications(_device(a), str(a.get("app") or ""), int(a.get("limit") or 15))
    if not items:
        return "No notifications."
    return "\n".join(f"[{_when(n['when'])}] {n['app']}: {n['title']}" + (f" — {n['text']}" if n["text"] else "") for n in items)


def adb_sms_read(a: dict) -> str:
    rows = comms.sms(_device(a), str(a.get("sender") or ""), int(a.get("limit") or 10))
    return "\n".join(f"[{_when(r['when'])}] {'(unread) ' if r['unread'] else ''}{r['from']}: {r['text']}"
                     for r in rows) or "No messages."


def adb_call_log(a: dict) -> str:
    rows = comms.calls(_device(a), int(a.get("limit") or 10))
    return "\n".join(f"[{_when(r['when'])}] {r['kind']} {r['name'] or r['number']}"
                     + (f" ({r['seconds']} s)" if r["seconds"] else "") for r in rows) or "No calls."


def adb_contacts(a: dict) -> str:
    hits = comms.contacts(_device(a), str(a.get("name", "")))
    return "\n".join(f"{h['name']}: {h['number']}" for h in hits) or "No matching contacts."


def adb_sms_send(a: dict) -> str:
    return comms.send_sms(_device(a), str(a.get("to", "")), str(a.get("text", ""))[:1000])


def adb_whatsapp_send(a: dict) -> str:
    return comms.send_whatsapp(_device(a), str(a.get("to", "")), str(a.get("text", ""))[:2000],
                               bool(a.get("business")))


def adb_call(a: dict) -> str:
    return comms.call(_device(a), str(a.get("to", "")))


def adb_call_answer(a: dict) -> str:
    return comms.call_key(_device(a), "answer")


def adb_call_end(a: dict) -> str:
    return comms.call_key(_device(a), "end")


def adb_watch(_: dict) -> list[dict]:
    """For the hub's hooks: each allowed device's current notifications and call state."""
    out = []
    for d in _devices():
        if d["state"] != "device" or not d["allowed"]:
            continue
        try:
            out.append({"serial": d["serial"], "model": d["model"],
                        "notifications": comms.notifications(d["id"], limit=30), "call": comms.call_state(d["id"])})
        except (ActionError, subprocess.TimeoutExpired):
            continue
    return out


ACTIONS = {f.__name__: f for f in (
    win_info, win_media, win_open, win_lock, win_screenshot,
    adb_devices, adb_info, adb_screenshot, adb_open_app, adb_key, adb_tap, adb_type,
    adb_shell, adb_install, adb_uninstall, adb_connect,
    adb_pair_start, adb_pair_status, adb_list, adb_allow,
    adb_notifications, adb_sms_read, adb_call_log, adb_contacts, adb_sms_send, adb_whatsapp_send,
    adb_call, adb_call_answer, adb_call_end, adb_watch)}


class Handler(BaseHTTPRequestHandler):
    server_version = "jarvis-device-bridge"

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(TOKEN) and hmac.compare_digest(got, TOKEN)

    def do_GET(self) -> None:
        if self.path != "/health":
            return self._send(404, {"detail": "not found"})
        if not self._authorised():
            return self._send(401, {"detail": "unauthorised"})
        self._send(200, {"status": "ok", "actions": sorted(ACTIONS), "adb_allowed": len(ADB_ALLOW),
                         "win_apps": sorted(WIN_APPS)})

    def do_POST(self) -> None:
        if self.path != "/v1/act":
            return self._send(404, {"detail": "not found"})
        if not self._authorised():
            return self._send(401, {"detail": "unauthorised"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_000:
                return self._send(413, {"detail": "request too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            name = str(body.get("action", ""))
            if name not in ACTIONS:
                return self._send(400, {"detail": f"unknown action {name!r}"})
            start = time.monotonic()
            result = ACTIONS[name](body.get("args") or {})
            log.info("action=%s ok secs=%.1f", name, time.monotonic() - start)
            self._send(200, {"result": result})
        except (ActionError, ValueError, TypeError) as e:
            log.info("action failed: %s", e)
            self._send(400, {"detail": str(e)})
        except subprocess.TimeoutExpired:
            self._send(504, {"detail": "the device took too long"})
        except FileNotFoundError:
            self._send(500, {"detail": f"adb not found at {ADB!r}; set ADB_PATH in .env"})
        except Exception:
            log.exception("action crashed")
            self._send(500, {"detail": "bridge error"})

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN or TOKEN == "change-me":
        raise SystemExit("set DEVICE_BRIDGE_TOKEN in .env first")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("device bridge on 127.0.0.1:%d; adb=%s; %d allowed Android serial(s); apps: %s",
             PORT, ADB, len(ADB_ALLOW), ", ".join(sorted(WIN_APPS)))
    server.serve_forever()


if __name__ == "__main__":
    main()
