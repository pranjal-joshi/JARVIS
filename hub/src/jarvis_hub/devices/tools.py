"""Device tools: this Windows laptop and Android phones/tablets over LAN ADB, via the device
bridge on the host (bridge/device_bridge.py), which enforces the app and device allow-lists.

Risk: looking (info, screenshots, device list) is a read; media keys, opening an app or URL,
locking and connecting are harmless writes; tapping, typing, shell, install and uninstall can
change anything on the phone, so they are destructive and wait for approval."""

import httpx

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Settings
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

DEVICE = {"device": {"type": "string", "description": "phone model or serial; empty if only one"}}
WIN_KEYS = ["play_pause", "next", "previous", "stop", "mute", "volume_up", "volume_down"]
ADB_KEYS = WIN_KEYS[:3] + ["volume_up", "volume_down", "mute", "home", "back", "wake", "sleep"]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


SPECS = [  # name, description, parameters, risk
    ("win_info", "This laptop: battery, memory, uptime and the active window.", _obj({}), Risk.READ),
    ("win_media", "Media or volume key on this laptop (times = repeat count for volume).",
     _obj({"key": {"type": "string", "enum": WIN_KEYS}, "times": {"type": "integer"}}, ["key"]), Risk.WRITE),
    ("win_open", "Open an allowed app (by name) or an https URL on this laptop.",
     _obj({"target": {"type": "string"}}, ["target"]), Risk.WRITE),
    ("win_lock", "Lock this laptop's screen.", _obj({}), Risk.WRITE),
    ("win_screenshot", "Save a screenshot of this laptop's screen.", _obj({}), Risk.READ),
    ("adb_devices", "List Android devices connected over ADB and whether each is allowed.", _obj({}), Risk.READ),
    ("adb_info", "An Android device's model, battery and foreground app.", _obj(DEVICE), Risk.READ),
    ("adb_screenshot", "Save a screenshot of an Android device.", _obj(DEVICE), Risk.READ),
    ("adb_open_app", "Open an app on an Android device by package or app name.",
     _obj({**DEVICE, "package": {"type": "string"}}, ["package"]), Risk.WRITE),
    ("adb_key", "Press a media, volume or navigation key on an Android device.",
     _obj({**DEVICE, "key": {"type": "string", "enum": ADB_KEYS}}, ["key"]), Risk.WRITE),
    ("adb_connect", "Reconnect to a phone's wireless debugging address (ip:port).",
     _obj({"address": {"type": "string"}}, ["address"]), Risk.WRITE),
    ("adb_notifications", "Recent notifications on a phone (WhatsApp, Messages, calls, mail, any app); "
     "filter by app name.", _obj({**DEVICE, "app": {"type": "string"}, "limit": {"type": "integer"}}), Risk.READ),
    ("adb_sms_read", "Read recent SMS on a phone, optionally only from one sender.",
     _obj({**DEVICE, "sender": {"type": "string"}, "limit": {"type": "integer"}}), Risk.READ),
    ("adb_call_log", "Recent calls on a phone: incoming, outgoing, missed.",
     _obj({**DEVICE, "limit": {"type": "integer"}}), Risk.READ),
    ("adb_contacts", "Look up a contact's phone number on a phone by name.",
     _obj({**DEVICE, "name": {"type": "string"}}, ["name"]), Risk.READ),
    ("adb_call_answer", "Answer the ringing call on a phone.", _obj(DEVICE), Risk.WRITE),
    ("adb_call_end", "Hang up the current call on a phone.", _obj(DEVICE), Risk.WRITE),
    ("adb_sms_send", "Send an SMS from a phone to a number or contact name. Needs approval.",
     _obj({**DEVICE, "to": {"type": "string"}, "text": {"type": "string"}}, ["to", "text"]), Risk.DESTRUCTIVE),
    ("adb_whatsapp_send", "Send a WhatsApp message from a phone to a number or contact name. Needs approval.",
     _obj({**DEVICE, "to": {"type": "string"}, "text": {"type": "string"}, "business": {"type": "boolean"}},
          ["to", "text"]), Risk.DESTRUCTIVE),
    ("adb_call", "Place a phone call to a number or contact name. Needs approval.",
     _obj({**DEVICE, "to": {"type": "string"}}, ["to"]), Risk.DESTRUCTIVE),
    ("adb_tap", "Tap a screen point on an Android device. Needs approval.",
     _obj({**DEVICE, "x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"]), Risk.DESTRUCTIVE),
    ("adb_type", "Type text into the focused field on an Android device. Needs approval.",
     _obj({**DEVICE, "text": {"type": "string"}}, ["text"]), Risk.DESTRUCTIVE),
    ("adb_shell", "Run a shell command on an Android device. Needs approval.",
     _obj({**DEVICE, "command": {"type": "string"}}, ["command"]), Risk.DESTRUCTIVE),
    ("adb_install", "Install an .apk from data/apk on an Android device. Needs approval.",
     _obj({**DEVICE, "apk": {"type": "string"}}, ["apk"]), Risk.DESTRUCTIVE),
    ("adb_uninstall", "Uninstall an app (exact package) from an Android device. Needs approval.",
     _obj({**DEVICE, "package": {"type": "string"}}, ["package"]), Risk.DESTRUCTIVE),
]


def register_device_tools(reg: ToolRegistry, s: Settings) -> bool:
    token = s.device_bridge_token.get_secret_value()
    if not token or token == "change-me":
        return False
    url = s.device_bridge_url.rstrip("/") + "/v1/act"

    def make(action: str):
        async def act(args: dict, ctx: ToolCtx) -> str:
            if action == "adb_install":  # the bridge only takes files from its own data/apk drop folder
                args = {**args, "apk": "data/apk/" + str(args.get("apk", "")).replace("\\", "/").split("/")[-1]}
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(200, connect=5)) as c:
                    r = await c.post(url, json={"action": action, "args": args},
                                     headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                return f"ERROR: the device bridge is not reachable ({type(e).__name__}). Is it running on the host?"
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200:
                return f"ERROR: {data.get('detail') or r.status_code}"
            return str(data.get("result", "Done."))
        return act

    for name, desc, params, risk in SPECS:
        reg.add(Tool(name, "device", desc, params, risk, make(name)))
    return True
