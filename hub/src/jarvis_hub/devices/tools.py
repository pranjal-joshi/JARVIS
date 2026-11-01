"""Device tools: this Windows laptop and Android phones/tablets over LAN ADB, via the device
bridge on the host (bridge/device_bridge.py), which enforces the app and device allow-lists.

Risk: looking (info, screenshots, device list) is a read; media keys, opening an app or URL,
locking and connecting are harmless writes; tapping, typing, shell, install and uninstall can
change anything on the phone, so they are destructive and wait for approval."""

import httpx

from jarvis_hub.approval.gate import Risk
from jarvis_hub.config import Settings
from jarvis_hub.mcp.registry import Tool, ToolCtx, ToolRegistry

WIN_KEYS = ["play_pause", "next", "previous", "stop", "mute", "volume_up", "volume_down"]
ADB_KEYS = WIN_KEYS[:3] + ["volume_up", "volume_down", "mute", "home", "back", "wake", "sleep"]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


# No `device` or `limit` parameters: the bridge picks the only phone and sensible counts itself, and small
# models fill optional fields with guesses. With several phones the bridge's error asks for `device`,
# which it still accepts.
S = {"type": "string"}
SPECS = [  # name, description, parameters, risk
    ("win_info", "This laptop's battery, memory and active window. Use for: 'laptop battery', "
     "'what's open on my laptop'.", _obj({}), Risk.READ),
    ("win_media", "Press a media or volume key on this laptop. Use for: 'pause the music', 'next song', "
     "'volume up' (times = how many presses).",
     _obj({"key": {"type": "string", "enum": WIN_KEYS}, "times": {"type": "integer"}}, ["key"]), Risk.WRITE),
    ("win_open", "Open an app or a website on this laptop. Use for: 'open Spotify', 'open youtube.com'.",
     _obj({"target": {"type": "string", "description": "app name or https URL"}}, ["target"]), Risk.WRITE),
    ("win_lock", "Lock this laptop. Use for: 'lock my laptop'.", _obj({}), Risk.WRITE),
    ("win_screenshot", "Save a screenshot of this laptop's screen.", _obj({}), Risk.READ),
    ("adb_devices", "Which phones and tablets are connected. Use for: 'is my phone connected'.", _obj({}), Risk.READ),
    ("adb_info", "The phone's battery and the app on screen. Use for: 'phone battery', 'what's on my phone'.",
     _obj({}), Risk.READ),
    ("adb_screenshot", "Save a screenshot of the phone's screen.", _obj({}), Risk.READ),
    ("adb_open_app", "Open an app on the phone. Use for: 'open YouTube on my phone'.",
     _obj({"package": {"type": "string", "description": "app name, e.g. YouTube"}}, ["package"]), Risk.WRITE),
    ("adb_key", "Press a key on the phone. Use for: 'pause my phone', 'go home on the phone', 'phone volume up'.",
     _obj({"key": {"type": "string", "enum": ADB_KEYS}}, ["key"]), Risk.WRITE),
    ("adb_connect", "Reconnect a phone by its wireless debugging address (ip:port).",
     _obj({"address": S}, ["address"]), Risk.WRITE),
    ("adb_notifications", "Recent phone notifications (WhatsApp, messages, calls, mail, any app). "
     "Use for: 'any new WhatsApps', 'what notifications do I have'.",
     _obj({"app": {"type": "string", "description": "only this app, e.g. WhatsApp; empty for all"}}), Risk.READ),
    ("adb_sms_read", "Read recent text messages (SMS). Use for: 'read my texts', 'any SMS from the bank'.",
     _obj({"sender": {"type": "string", "description": "only from this sender; empty for all"}}), Risk.READ),
    ("adb_call_log", "Recent calls: incoming, outgoing, missed. Use for: 'who called me', 'missed calls'.",
     _obj({}), Risk.READ),
    ("adb_contacts", "Look up a contact's number. Use for: 'what's Mum's number'.",
     _obj({"name": S}, ["name"]), Risk.READ),
    ("adb_call_answer", "Answer the ringing phone. Use for: 'answer it', 'pick up'.", _obj({}), Risk.WRITE),
    ("adb_call_end", "Hang up or reject the current call. Use for: 'hang up', 'reject the call'.",
     _obj({}), Risk.WRITE),
    ("adb_sms_send", "Send a text message (SMS). Use for: 'text Mum I'm on my way'. Needs approval.",
     _obj({"to": {"type": "string", "description": "contact name or number"}, "text": S}, ["to", "text"]),
     Risk.DESTRUCTIVE),
    ("adb_whatsapp_send", "Send a WhatsApp message. Use for: 'WhatsApp Rohan see you at 8'. Needs approval.",
     _obj({"to": {"type": "string", "description": "contact name or number"}, "text": S}, ["to", "text"]),
     Risk.DESTRUCTIVE),
    ("adb_call", "Phone someone. Use for: 'call Mum', 'ring 98200 12345'. Needs approval.",
     _obj({"to": {"type": "string", "description": "contact name or number"}}, ["to"]), Risk.DESTRUCTIVE),
    # hidden from the model by default (MODEL_HIDDEN_TOOLS): they can do anything on the phone
    ("adb_tap", "Tap a screen point on the phone. Needs approval.",
     _obj({"x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"]), Risk.DESTRUCTIVE),
    ("adb_type", "Type text into the focused field on the phone. Needs approval.",
     _obj({"text": S}, ["text"]), Risk.DESTRUCTIVE),
    ("adb_shell", "Run a shell command on the phone. Needs approval.",
     _obj({"command": S}, ["command"]), Risk.DESTRUCTIVE),
    ("adb_install", "Install an .apk from data/apk on the phone. Needs approval.",
     _obj({"apk": S}, ["apk"]), Risk.DESTRUCTIVE),
    ("adb_uninstall", "Uninstall an app (exact package) from the phone. Needs approval.",
     _obj({"package": S}, ["package"]), Risk.DESTRUCTIVE),
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
