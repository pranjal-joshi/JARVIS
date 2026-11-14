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

import base64
import ctypes
import difflib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from android_comms import Comms
import phone_routine as routine

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


# Wireless devices that stop answering are dropped (adb disconnect) after DROP_AFTER failed checks in a
# row, so a phone that left the Wi-Fi or turned debugging off doesn't linger as "offline". USB devices and
# ones waiting on the phone's "Allow USB debugging?" prompt (unauthorized) are never dropped.
DROP_AFTER = 2
_misses: dict[str, int] = {}


def _wireless(dev_id: str) -> bool:
    return ":" in dev_id or "._adb-tls-connect." in dev_id


def _unreachable(dev_id: str) -> None:
    _misses[dev_id] = _misses.get(dev_id, 0) + 1
    if _misses[dev_id] >= DROP_AFTER:
        try:
            adb("disconnect", dev_id, timeout=10)
            log.info("action=adb_drop ok (unreachable)")
        except (ActionError, subprocess.TimeoutExpired):
            pass
        _misses.pop(dev_id, None)


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
                d["serial"] = adb("shell", "getprop", "ro.serialno", serial=d["id"], timeout=8)
                _misses.pop(d["id"], None)
            except (ActionError, subprocess.TimeoutExpired):
                d["serial"] = ""
                if _wireless(d["id"]):
                    _unreachable(d["id"])
        elif d["state"] == "offline" and _wireless(d["id"]):
            _unreachable(d["id"])
        d["allowed"] = d.get("serial", "") in _allowed()
        out.append(d)
    return out


def _device(a: dict) -> str:
    """The adb id of an allowed device: the one named (model, serial or id), or the only one."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())
    want = norm(a.get("device") or "")
    if want in ("none", "null", "phone", "myphone", "default", "any"):
        want = ""
    allowed = [d for d in _devices() if d["allowed"]]
    if not allowed:
        raise ActionError("no phone is connected and allowed. Tell the user to connect it from the HUD Devices "
                          "panel; don't retry.")
    ok = allowed
    if want:
        named = [d for d in allowed if want in (norm(d["id"]), norm(d.get("serial", "")))
                 or want in norm(d["model"]) or norm(d["model"]) in want]
        # a small model often invents a device name ("user_phone", "Pixel 9"); with one phone that's harmless
        ok = named or (allowed if len(allowed) == 1 else [])
    if not ok:
        raise ActionError("no connected phone matches that name. Call the same tool again with device set to "
                          "one of: " + ", ".join(d["model"] or d["id"] for d in allowed))
    if len(ok) > 1:
        raise ActionError("several phones are connected. Call the same tool again with device set to one of: "
                          + ", ".join(d["model"] or d["id"] for d in ok))
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
    want = str(a.get("package", "")).strip()
    # only apps with a launcher icon; the model often guesses package names, so check every guess
    out = adb("shell", "cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
              "-c", "android.intent.category.LAUNCHER", serial=s)
    apps = sorted({l.strip().split("/")[0] for l in out.splitlines() if "/" in l})
    if want not in apps:
        key = re.sub(r"[^a-z0-9]", "", want.lower().rsplit(".", 1)[-1] if PKG.match(want) else want.lower())
        hits = [p for p in apps if key and key in re.sub(r"[^a-z0-9]", "", p.lower())]
        if len(hits) != 1:
            close = hits or difflib.get_close_matches(key, apps, n=5, cutoff=0.3)
            raise ActionError(f"no single app matches {want!r}; candidates: {', '.join(close[:6]) or 'none'}")
        want = hits[0]
    adb("shell", "monkey", "-p", want, "-c", "android.intent.category.LAUNCHER", "1", serial=s)
    return f"Opened {want}."


KEYS = {"play_pause": 85, "next": 87, "previous": 88, "volume_up": 24, "volume_down": 25, "mute": 164,
        "home": 3, "back": 4, "wake": 224, "sleep": 223}


def adb_key(a: dict) -> str:
    s = _device(a)
    key = str(a.get("key", ""))
    if key not in KEYS:
        raise ActionError(f"key must be one of {sorted(KEYS)}")
    before = _screen_hash(s)
    adb("shell", "input", "keyevent", str(KEYS[key]), serial=s)
    return f"Sent {key}." + _settle(s, before)


def adb_tap(a: dict) -> str:
    s = _device(a)
    x, y = int(a.get("x")), int(a.get("y"))
    before = _screen_hash(s)
    adb("shell", "input", "tap", str(x), str(y), serial=s)
    return f"Tapped {x},{y}." + _settle(s, before)


# ---------- unsticking: a pop-up in the way, or a screen that didn't move ----------
def _screen_hash(s: str) -> str:
    """A fingerprint of what's on screen (the clock or a blinking cursor can change it: equal means stuck)."""
    try:
        return adb("shell", "screencap | md5sum", serial=s)[:32]
    except ActionError:
        return ""


def _clear_popup(s: str) -> str:
    """Press the safe button of a known system pop-up on screen (phone_routine.POPUPS); what was pressed, or ""."""
    try:
        adb("shell", "uiautomator", "dump", "/sdcard/jarvis_ui.xml", serial=s)
        hit = routine.find_popup(adb("shell", "cat", "/sdcard/jarvis_ui.xml", serial=s))
    except ActionError:
        return ""
    if not hit:
        return ""
    label, x, y = hit
    adb("shell", "input", "tap", str(x), str(y), serial=s)
    time.sleep(0.8)
    return label


def _settle(s: str, before: str) -> str:
    """After an action: a note when the screen didn't change, after closing a pop-up that was in the way."""
    time.sleep(0.8)
    if not before or _screen_hash(s) != before:
        return ""
    time.sleep(1.2)  # slow apps: one more look before calling it stuck
    if _screen_hash(s) != before:
        return ""
    if label := _clear_popup(s):
        return f' A pop-up was in the way; pressed "{label}". Try the action again.'
    return " The screen didn't change: the tap may have missed or the app is stuck."


# ---------- recording what the user does by hand, and replaying it ----------
_recording: dict = {}


def adb_record_start(a: dict) -> str:
    s = _device(a)
    if _recording.get("proc") and _recording["proc"].poll() is None:
        raise ActionError("already recording; stop that first")
    touch = routine.touch_range(adb("shell", "getevent", "-lp", serial=s))
    if not touch:
        raise ActionError("couldn't find the phone's touchscreen")
    m = re.search(r"(\d+)x(\d+)\s*$", adb("shell", "wm", "size", serial=s))
    if not m:
        raise ActionError("couldn't read the screen size")
    focus = adb("shell", "dumpsys", "window", serial=s)
    app = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}/ ]+)", focus)
    out = SHOTS.parent / "routine-recording.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    f = open(out, "w", encoding="utf-8")
    proc = subprocess.Popen([ADB, "-s", s, "shell", "getevent", "-lt"], stdout=f, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, text=True)
    _recording.update(proc=proc, file=f, path=out, serial=s, touch=touch, size=(int(m[1]), int(m[2])),
                      app=app[1] if app else "", started=time.time())
    return (f"Recording what you do on the phone{f' (starting in {app[1]})' if app else ''}. "
            "Say stop recording when you're done.")


def adb_record_stop(_: dict) -> dict:
    proc = _recording.get("proc")
    if not proc:
        raise ActionError("nothing is being recorded")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    _recording["file"].close()
    lines = _recording["path"].read_text(encoding="utf-8", errors="replace").splitlines()
    steps = routine.parse_getevent(lines, _recording["touch"], _recording["size"])
    if _recording.get("app") and "launcher" not in _recording["app"].lower():
        steps = [f"open {_recording['app']}", "wait 2.0s"] + steps
    took = round(time.time() - _recording["started"])
    _recording.clear()
    return {"steps": steps, "seconds": took}


def adb_replay(a: dict) -> str:
    """Do a recorded routine's steps; after each tap or swipe, a pop-up in the way is closed and the step tried
    once more, and a screen that stays stuck stops the routine there."""
    s = _device(a)
    steps = routine.parse_steps([str(x) for x in a.get("steps") or []])
    for i, step in enumerate(steps, 1):
        kind, args = step[0], step[1:]
        if kind == "wait":
            time.sleep(args[0])
            continue
        if kind == "open":
            adb_open_app({"package": args[0], "device": s})
            continue
        if kind == "key":
            if args[0] not in KEYS:
                raise ActionError(f"step {i}: unknown key {args[0]}")
            adb("shell", "input", "keyevent", str(KEYS[args[0]]), serial=s)
            continue
        cmd = ["input", "tap", *map(str, args)] if kind == "tap" else             ["input", "swipe", *map(str, args[:4]), str(args[4] if len(args) > 4 else 300)]
        before = _screen_hash(s)
        adb("shell", *cmd, serial=s)
        note = _settle(s, before)
        if "pop-up" in note:
            before = _screen_hash(s)
            adb("shell", *cmd, serial=s)
            note = _settle(s, before)
        if "didn't change" in note:
            return f"Stopped at step {i} of {len(steps)} ({kind} {' '.join(map(str, args))}): the screen didn't change."
    return f"Done: {len(steps)} steps."


def adb_type(a: dict) -> str:
    s = _device(a)
    text = str(a.get("text", ""))[:500]
    if "\n" in text or "\r" in text:  # a newline ends `input text` and runs the rest as a phone shell command
        raise ActionError("text with line breaks can't be typed; send each line separately")
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


def adb_disconnect(a: dict) -> str:
    """Drop a wireless device (HUD only, not a model tool). It can reconnect from Wireless debugging."""
    dev_id = str(a.get("id", ""))
    if dev_id not in {d["id"] for d in _devices()}:
        raise ActionError("no such device connected")
    if not _wireless(dev_id):
        raise ActionError("that device is on USB; unplug it instead")
    adb("disconnect", dev_id, timeout=10)
    _misses.pop(dev_id, None)
    return "Disconnected."


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


# ---------- files: read-only, inside folders you list, nothing else ----------
# DEVICE_FILES_ALLOW=notes=C:\Users\me\Documents\Notes;recipes=D:\Recipes  (name=folder, ";" between),
# or * for every local drive (named by letter: c, d...). Paths may also be absolute (C:\Users\me\x.txt).
# Empty = the file tools find nothing. There is no write, move or delete here, whatever is listed.


def _file_roots(spec: str) -> dict[str, Path]:
    roots = {}
    for part in (p.strip() for p in spec.split(";") if p.strip()):
        if part == "*":
            roots |= {d.lower(): Path(f"{d}:\\") for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      if ctypes.windll.kernel32.GetDriveTypeW(f"{d}:\\") == 3}  # 3 = a fixed (local) disk
        elif "=" in part:
            k, v = part.split("=", 1)
            roots[k.strip().lower()] = Path(v.strip()).resolve()
    return roots


FILE_ROOTS = _file_roots(CFG.get("DEVICE_FILES_ALLOW", ""))
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".log", ".ini", ".toml", ".cfg",
            ".conf", ".xml", ".html", ".htm", ".css", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".ps1", ".psm1",
            ".bat", ".cmd", ".sh", ".sql", ".java", ".cs", ".go", ".rs", ".c", ".cpp", ".h", ".rb", ".php", ".srt",
            ".vtt", ".tex", ".rst", ".gitignore", ".dockerfile"}
# credentials stay unreadable even with whole drives shared: key files, password stores, auth configs
SECRET = re.compile(r"(^\.env|\.pem$|\.key$|\.pfx$|\.p12$|id_rsa|id_ed25519|id_ecdsa|password|secret|credential|"
                    r"token|\.kdbx$|wallet|^\.npmrc$|^\.pypirc$|^\.netrc$|^_netrc$|^\.git-credentials$|"
                    r"^\.ssh$|^\.gnupg$|^\.aws$|^\.azure$|^\.kube$|^\.docker$|cookies|login data|"
                    r"^local state$|\.rdp$)", re.I)
MAX_FILE_BYTES = 200_000
# folder names people (and small models) say, under this user's profile; still only readable if a shared
# folder (or a shared drive) holds them
KNOWN_FOLDERS = {n.lower(): n for n in ("Downloads", "Documents", "Desktop", "Pictures", "Music", "Videos")}


def _in_roots(rel: str) -> tuple[str, Path]:
    """'notes/sub/file.md' or 'C:\\Users\\me\\file.md' -> (root name, absolute path), refusing anything
    outside the listed folders and anything that looks like a credential."""
    if not FILE_ROOTS:
        raise ActionError("no folders are shared with JARVIS (DEVICE_FILES_ALLOW is empty)")
    rel = rel.strip().strip('"')
    head, _, tail = rel.replace("\\", "/").strip("/ ").partition("/")
    if head.lower() in KNOWN_FOLDERS and head.lower() not in FILE_ROOTS:  # "Downloads" -> this user's Downloads
        rel = str(Path.home() / KNOWN_FOLDERS[head.lower()] / tail)
    if re.match(r"^[A-Za-z]:([\\/]|$)", rel):  # an absolute path: find the shared folder holding it
        path = Path(rel if len(rel) > 2 else rel + "\\").resolve()
        name, root = next(((n, r) for n, r in sorted(FILE_ROOTS.items(), key=lambda kv: -len(str(kv[1])))
                           if path == r or r in path.parents), (None, None))
        if root is None:
            raise ActionError(f"{path} isn't in a shared folder. Shared: {', '.join(sorted(FILE_ROOTS))}")
    else:
        rel = rel.replace("\\", "/").strip("/ ")
        name, _, rest = rel.partition("/")
        root = FILE_ROOTS.get(name.lower().rstrip(":"))
        if root is None:
            raise ActionError(f"no shared folder called {name!r}. Shared: {', '.join(sorted(FILE_ROOTS))}")
        path = (root / rest).resolve()
    if path != root and root not in path.parents:
        raise ActionError("that path leaves the shared folder")
    if any(SECRET.search(part) for part in path.relative_to(root).parts):
        raise ActionError("that file looks like a secret; it isn't shared")
    return name, path


def fs_list(a: dict) -> str:
    if not str(a.get("path", "")).strip():
        return ("Shared folders: " + ", ".join(f"{n} ({r})" for n, r in sorted(FILE_ROOTS.items()))
                if FILE_ROOTS else "No folders are shared.")
    name, path = _in_roots(str(a["path"]))
    if not path.is_dir():
        raise ActionError("not a folder")
    items = sorted((p for p in path.iterdir() if not p.name.startswith(".") and not SECRET.search(p.name)),
                   key=lambda p: (p.is_file(), p.name.lower()))[:100]
    return "\n".join(f"{p.name}/" if p.is_dir() else f"{p.name} ({p.stat().st_size // 1024} KB)" for p in items) \
        or "(empty)"


def fs_read(a: dict) -> str:
    name, path = _in_roots(str(a.get("path", "")))
    if not path.is_file():
        raise ActionError("no such file")
    if (path.suffix or path.name).lower() not in TEXT_EXT and path.name.lower() not in ("dockerfile", "makefile",
                                                                                       "readme", "license"):
        raise ActionError(f"only text files ({', '.join(sorted(TEXT_EXT))})")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ActionError("that file is too big to read aloud")
    return path.read_text(encoding="utf-8", errors="replace")[:6000]


# ---------- PowerShell: look-only commands run freely, others after your approval, file changes never ----------
# bridge/ps_classify.ps1 judges each command with PowerShell's own parser (aliases resolved): look-only
# (Get-/Test-/Measure-... and a few tools like nvidia-smi), changes (Stop-Process, Start-Process...), or
# blocked (writes, moves or deletes files, nested shells, iex, encoded commands). The hub asks you before
# win_ps_run; this check runs again here, so a blocked command never runs whatever the hub decided.
PWSH = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
PS_CLASSIFY = base64.b64encode((Path(__file__).with_name("ps_classify.ps1")).read_text(encoding="utf-8")
                               .encode("utf-16-le")).decode()
PS_TIMEOUT = int(CFG.get("POWERSHELL_TIMEOUT_S", "60") or 60)


def _ps(args: list[str], timeout: int, env: dict | None = None) -> str:
    p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                       env={**os.environ, **(env or {})}, cwd=Path.home(), creationflags=0x08000000)  # no window
    return (p.stdout + (p.stderr if p.returncode else "")).strip()


def _ps_classify(cmd: str) -> dict:
    """{"read": bool, "block": str|None, "why": str|None} from bridge/ps_classify.ps1; raises if it won't parse."""
    try:
        verdict = json.loads(_ps(["-EncodedCommand", PS_CLASSIFY], 30, {"JARVIS_PS_CMD": cmd}).splitlines()[-1])
    except (ValueError, IndexError):
        raise ActionError("the command couldn't be checked, so it wasn't run")
    if verdict.get("error"):
        raise ActionError(f"that isn't valid PowerShell: {verdict['error']}")
    if verdict.get("block"):
        raise ActionError(f"not allowed: the command {verdict['block']}. Files can't be written, moved or deleted")
    return verdict


def _ps_check(cmd: str) -> dict:
    cmd = cmd.strip()
    if not cmd:
        raise ActionError("empty command")
    if len(cmd) > 2000:
        raise ActionError("that command is too long")
    if any(SECRET.search(w) for w in re.split(r"[\\/\s'\"(),;|]+", cmd) if w):
        raise ActionError("that command touches something that looks like a credential; it isn't allowed")
    return _ps_classify(cmd)


def _ps_exec(cmd: str) -> str:
    script = f"& {{\n{cmd}\n}} 2>&1 | Out-String -Width 160"
    out = _ps(["-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()], PS_TIMEOUT)
    return (out[:4000] + ("\n(… cut)" if len(out) > 4000 else "")) or "(no output)"


def win_ps_read(a: dict) -> str:
    cmd = str(a.get("command", ""))
    v = _ps_check(cmd)
    if not v.get("read"):
        raise ActionError(f"that command changes things ({v.get('why') or 'not look-only'}); "
                          "use powershell_run, which asks the user first")
    return _ps_exec(cmd)


def win_ps_run(a: dict) -> str:
    cmd = str(a.get("command", ""))
    _ps_check(cmd)
    return _ps_exec(cmd)


# ---------- notifications on this laptop and the phone ----------
# A Windows toast through the built-in WinRT API (Windows PowerShell 5.1; nothing to install). Title and text
# travel as environment variables, never inside the script, so they can't inject PowerShell.
TOAST = base64.b64encode(r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$e = { param($s) [System.Security.SecurityElement]::Escape([string]$s) }
$launch = if ($env:JARVIS_TOAST_URL) { " activationType=`"protocol`" launch=`"$(& $e $env:JARVIS_TOAST_URL)`"" } else { '' }
$xml = "<toast$launch><visual><binding template=`"ToastGeneric`"><text>$(& $e $env:JARVIS_TOAST_TITLE)</text>" +
       "<text>$(& $e $env:JARVIS_TOAST_TEXT)</text></binding></visual></toast>"
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument; $doc.LoadXml($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
  [Windows.UI.Notifications.ToastNotification]::new($doc))
""".encode("utf-16-le")).decode()


def win_notify(a: dict) -> str:
    title, text = str(a.get("title") or "JARVIS")[:120], str(a.get("text") or "")[:400]
    url = str(a.get("url") or "")
    url = url if re.match(r"^https://[^\s\"<>]+$", url) else ""
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", TOAST],
                       capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                       env={**os.environ, "JARVIS_TOAST_TITLE": title, "JARVIS_TOAST_TEXT": text,
                            "JARVIS_TOAST_URL": url}, creationflags=0x08000000)
    if p.returncode:
        raise ActionError(f"the toast failed: {(p.stderr or p.stdout).strip()[-200:]}")
    return "Shown on this laptop."


def adb_notify(a: dict) -> str:
    """A notification on the phone (Android 10+), posted by the adb shell user."""
    s = _device(a)
    title, text = str(a.get("title") or "JARVIS")[:120], str(a.get("text") or "")[:400]
    q = lambda v: "'" + v.replace("'", "'\\''") + "'"
    adb("shell", f"cmd notification post -S bigtext -t {q(title)} jarvis-{int(time.time())} {q(text)}", serial=s)
    return "Shown on the phone."


# ---------- watchers: look-only checks you define, run here on a schedule by the hub ----------
# They live in config/watchers.json, which only this bridge reads and writes, and only through the
# watch_save / watch_delete actions the HUD calls (the model has no tool for them). A command the
# PowerShell check blocks (file changes, nested shells, iex) is never saved or run. Each command prints
# JSON (an array of objects, or one object); `key` names the field that identifies an item.
WATCH_FILE = ROOT / "config" / "watchers.json"
WATCH_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
WATCH_EVERY = re.compile(r"^(\d+)\s*([mh])$", re.I)
WATCH_TARGETS = {"windows", "android", "hud", "voice"}
# JARVIS's own keys never reach a watcher command
NOT_FOR_WATCHERS = re.compile(r"(HUB_TOKEN|BRIDGE_TOKEN|HA_TOKEN|ANTHROPIC|LLM_API_KEY)", re.I)
_watch_lock = threading.Lock()


def _watchers() -> list[dict]:
    try:
        data = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [w for w in data if isinstance(w, dict)] if isinstance(data, list) else []


def _watch_check(w: dict) -> dict:
    """Validated copy of a watcher, with `verified` (look-only by the PowerShell check) set."""
    if not isinstance(w, dict):
        raise ActionError("a watcher is an object")
    name = str(w.get("name", "")).strip().lower()
    if not WATCH_NAME.match(name):
        raise ActionError("name: 2-41 characters, lower-case letters, digits and -")
    m = WATCH_EVERY.match(str(w.get("every", "")).strip())
    if not m or not 1 <= int(m[1]) * (1 if m[2].lower() == "m" else 60) <= 24 * 60:
        raise ActionError("every: like 5m or 2h, between 1 minute and 24 hours")
    shell = str(w.get("shell") or "pwsh")
    if shell not in ("pwsh", "powershell"):
        raise ActionError("shell: pwsh (PowerShell 7) or powershell (Windows PowerShell 5.1)")
    command = str(w.get("command", "")).strip()
    if not command or len(command) > 4000:
        raise ActionError("command: 1-4000 characters")
    env = [str(e).strip() for e in (w.get("env") or []) if str(e).strip()]
    for e in env:
        if not re.match(r"^[A-Z][A-Z0-9_]{1,60}$", e) or NOT_FOR_WATCHERS.search(e):
            raise ActionError(f"env: {e!r} can't be passed to a watcher")
    notify = [t for t in (w.get("notify") or []) if t in WATCH_TARGETS]
    verdict = _ps_classify(command)  # raises for blocked commands
    return {"name": name, "enabled": bool(w.get("enabled", True)), "every": f"{m[1]}{m[2].lower()}",
            "shell": shell, "command": command, "key": str(w.get("key") or "id").strip(),
            "title": str(w.get("title") or name)[:80], "text": str(w.get("text") or "{" + str(w.get("key") or "id") + "}")[:300],
            "url": str(w.get("url") or "")[:300], "notify": notify, "env": env,
            "description": str(w.get("description") or "")[:300],
            "verified": bool(verdict.get("read")), "why": verdict.get("why") or ""}


def _fill(template: str, item: dict) -> str:
    def field(m):
        v = item
        for part in m[1].split("."):
            v = v.get(part, "") if isinstance(v, dict) else ""
        return str(v if v is not None else "")
    return re.sub(r"\{([\w.]+)\}", field, template)


def _watch_exec(w: dict) -> dict:
    exe = PWSH if w["shell"] == "pwsh" else "powershell.exe"
    env = {e: CFG.get(e, os.environ.get(e, "")) for e in w["env"]}
    script = f"& {{\n{w['command']}\n}} | ConvertTo-Json -Depth 4 -Compress"
    if re.search(r"ConvertTo-Json|--json|-o json|--output json", w["command"], re.I):
        script = w["command"]  # it already prints JSON
    p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                        base64.b64encode(script.encode("utf-16-le")).decode()],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=PS_TIMEOUT,
                       stdin=subprocess.DEVNULL, env={**os.environ, **env}, cwd=Path.home(),
                       creationflags=0x08000000)
    out = p.stdout.strip()
    if p.returncode and not out:
        raise ActionError(f"the command failed: {(p.stderr or '').strip()[-300:]}")
    if not out:
        return {"items": [], "count": 0}
    start = min([i for i in (out.find("["), out.find("{")) if i >= 0] or [0])
    try:
        data = json.loads(out[start:])
    except ValueError:
        raise ActionError(f"the command didn't print JSON: {out[:200]}")
    rows = data if isinstance(data, list) else [data]
    items = []
    for row in rows[:200]:
        if not isinstance(row, dict):
            row = {"value": row}
        key = _fill("{" + w["key"] + "}", row) or json.dumps(row, sort_keys=True)[:200]
        items.append({"key": key[:300], "text": _fill(w["text"], row)[:300], "url": _fill(w["url"], row)[:300]})
    return {"items": items, "count": len(items)}


def watch_list(_: dict) -> list[dict]:
    return _watchers()


def watch_run(a: dict) -> dict:
    w = next((w for w in _watchers() if w.get("name") == str(a.get("name", "")).lower()), None)
    if not w:
        raise ActionError("no watcher by that name")
    return _watch_exec(_watch_check(w))


def watch_test(a: dict) -> dict:
    """Check and run a watcher that isn't saved (the HUD's Test button, an expert's draft)."""
    w = _watch_check(a.get("watcher"))
    return {"watcher": w, **_watch_exec(w)}


def watch_save(a: dict) -> dict:
    w = _watch_check(a.get("watcher"))
    if not w["verified"] and not a.get("accept_unverified"):
        raise ActionError(f"not verified look-only ({w['why']}); save it only if you trust it (accept_unverified)")
    with _watch_lock:
        rest = [x for x in _watchers() if x.get("name") not in (w["name"], str(a.get("replaces") or ""))]
        WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        WATCH_FILE.write_text(json.dumps(sorted([*rest, w], key=lambda x: x["name"]), indent=1), encoding="utf-8")
    return w


def watch_delete(a: dict) -> str:
    name = str(a.get("name", "")).lower()
    with _watch_lock:
        all_ = _watchers()
        if not any(x.get("name") == name for x in all_):
            raise ActionError("no watcher by that name")
        WATCH_FILE.write_text(json.dumps([x for x in all_ if x.get("name") != name], indent=1), encoding="utf-8")
    return f"Deleted {name}."


ACTIONS = {f.__name__: f for f in (
    fs_list, fs_read, win_ps_read, win_ps_run, win_notify, adb_notify,
    watch_list, watch_run, watch_test, watch_save, watch_delete,
    win_info, win_media, win_open, win_lock, win_screenshot,
    adb_devices, adb_info, adb_screenshot, adb_open_app, adb_key, adb_tap, adb_type,
    adb_shell, adb_install, adb_uninstall, adb_connect,
    adb_pair_start, adb_pair_status, adb_list, adb_allow, adb_disconnect,
    adb_notifications, adb_sms_read, adb_call_log, adb_contacts, adb_sms_send, adb_whatsapp_send,
    adb_call, adb_call_answer, adb_call_end, adb_watch, adb_record_start, adb_record_stop, adb_replay)}


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
        return bool(TOKEN) and hmac.compare_digest(got.encode(), TOKEN.encode())  # str compare raises on non-ASCII

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
        body = {}
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
            log.info("action=%s failed (%s)", body.get("action") if isinstance(body, dict) else "?", type(e).__name__)  # no details: they can hold names and numbers
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
