"""Phone communications over ADB for the device bridge: notifications, SMS, call log, contacts,
sending SMS / WhatsApp messages and placing calls. No root and no app on the phone: everything uses
`adb shell` (content providers, dumpsys, activity intents, and a UI dump to press "Send").

Pure helpers (parsers) are kept free of adb so they can be tested on sample output.
"""

import difflib
import re
import shlex
import time
from urllib.parse import quote

APPS = {
    "com.whatsapp": "WhatsApp", "com.whatsapp.w4b": "WhatsApp Business",
    "com.google.android.apps.messaging": "Messages", "com.samsung.android.messaging": "Messages",
    "com.android.mms": "Messages", "com.google.android.dialer": "Phone", "com.android.dialer": "Phone",
    "com.samsung.android.dialer": "Phone", "com.android.server.telecom": "Phone",
    "com.google.android.gm": "Gmail", "com.microsoft.office.outlook": "Outlook",
    "com.microsoft.teams": "Teams", "com.slack": "Slack", "org.telegram.messenger": "Telegram",
    "com.google.android.calendar": "Calendar", "com.instagram.android": "Instagram",
    "com.google.android.apps.nbu.paisa.user": "GPay", "net.one97.paytm": "Paytm", "com.phonepe.app": "PhonePe",
    "com.dreamplug.androidapp": "CRED", "com.ubercab": "Uber", "com.olacabs.customer": "Ola",
    "in.amazon.mShop.android.shopping": "Amazon", "com.myairtelapp": "Airtel", "com.jio.myjio": "Jio",
    "com.google.android.googlequicksearchbox": "Google", "com.google.android.apps.photos": "Photos",
    "com.google.android.apps.weather": "Weather", "com.discord": "Discord", "com.huami.watch.hmwatchmanager": "Zepp",
    "com.realbyteapps.moneymanagerfree": "Money Manager", "com.google.android.youtube": "YouTube",
}
# system chatter nobody asks about
NOISE = ("android", "com.android.systemui", "com.android.vending", "com.google.android.gms",
         "com.android.providers.downloads", "com.google.android.apps.wellbeing")
SUMMARY = re.compile(r"^(\d+ new (messages?|notifications?)|\d+ messages? from \d+ chats?|you have a notification)$", re.I)
NUMBER = re.compile(r"^\+?[\d\s()-]{5,20}$")


def app_name(pkg: str) -> str:
    return APPS.get(pkg, pkg.rsplit(".", 1)[-1].capitalize())


def sh(*args: str) -> str:
    """One `adb shell` command line with every argument quoted for the phone's /system/bin/sh."""
    return " ".join(shlex.quote(a) for a in args)


# ---------------------------------------------------------------- parsers
_EXTRA = r"android\.{key}=\w+ \((.*?)\)\s*$"


def parse_notifications(dump: str) -> list[dict]:
    """`dumpsys notification --noredact` -> [{key, pkg, app, title, text, when}] (newest first)."""
    out = []
    for block in re.split(r"\n\s*NotificationRecord\(", dump)[1:]:
        pkg = re.search(r"pkg=(\S+)", block)
        key = re.search(r"key=(\S+?):", block) or re.search(r"key=(\S+)", block)
        if not pkg or pkg[1] in NOISE:
            continue
        get = lambda k: (m[1].strip() if (m := re.search(_EXTRA.format(key=k), block, re.M)) else "")
        title, text = get("title"), get("bigText") or get("text")
        if not (title or text):
            continue
        # group summaries ("WhatsApp: 16 messages from 5 chats", "GPay: You have a notification") repeat
        # the real entries and confuse a small model: drop them
        if SUMMARY.match(text) and (title.lower() in (app_name(pkg[1]).lower(), "") or SUMMARY.match(title)):
            continue
        when = re.search(r"\bwhen=(\d{12,})", block)
        out.append({"key": key[1] if key else f"{pkg[1]}:{title}", "pkg": pkg[1], "app": app_name(pkg[1]),
                    "title": title, "text": text[:400], "when": int(when[1]) // 1000 if when else 0})
    out.sort(key=lambda n: n["when"], reverse=True)
    return out


def parse_rows(output: str, fields: list[str]) -> list[dict]:
    """`content query` output -> rows. The last field may contain ", " (message bodies)."""
    rows = []
    head = ", ".join(f"{f}=(.*?)" for f in fields[:-1])
    pat = re.compile(rf"^Row: \d+ {head}, {fields[-1]}=(.*)$" if head else rf"^Row: \d+ {fields[-1]}=(.*)$")
    for line in output.splitlines():
        if m := pat.match(line.strip()):
            rows.append({f: ("" if v == "NULL" else v) for f, v in zip(fields, m.groups())})
    return rows


def _plain(name: str) -> str:
    """Lowercase letters/digits/spaces only: "Sanika ❤️" -> "sanika"."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", name.lower())).strip()


def rank_contacts(rows: list[dict], query: str) -> tuple[int, list[dict]]:
    """Best tier of contacts matching `query`, with its hits (unique name+number):
    1 the whole name, 2 a word of the name before any "(nickname)", 3 anywhere (nicknames too),
    4 a close spelling only (a suggestion), 0 nothing."""
    q = _plain(query)
    tiers: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: []}
    seen = set()
    for r in rows:
        name, num = r["display_name"], re.sub(r"[\s()-]", "", r["data1"])
        if not q or (name, num) in seen:
            continue
        seen.add((name, num))
        full, main = _plain(name), _plain(name.split("(")[0])
        hit = {"name": name, "number": num}
        if full == q or main == q:
            tiers[1].append(hit)
        elif re.search(rf"\b{re.escape(q)}\b", main):
            tiers[2].append(hit)
        elif q in full:
            tiers[3].append(hit)
        elif any(difflib.SequenceMatcher(None, q, w).ratio() >= 0.8 for w in [main, *main.split()]):
            tiers[4].append(hit)
    for t in (1, 2, 3, 4):
        if tiers[t]:
            return t, tiers[t]
    return 0, []


def find_button(xml: str, labels: tuple[str, ...]) -> tuple[int, int] | None:
    """Centre of the first clickable node whose text or content-desc starts with one of `labels`."""
    for node in re.finditer(r"<node [^>]*>", xml):
        n = node[0]
        names = [(m[1] or "").strip().lower() for m in re.finditer(r'(?:text|content-desc)="([^"]*)"', n)]
        if any(nm and nm.startswith(lb.lower()) for nm in names for lb in labels):
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
            if b:
                x1, y1, x2, y2 = map(int, b.groups())
                return (x1 + x2) // 2, (y1 + y2) // 2
    return None


# ---------------------------------------------------------------- actions (adb is passed in)
class Comms:
    def __init__(self, adb, error):
        self.adb, self.Error = adb, error

    def shell(self, serial: str, *args: str, timeout: int = 20) -> str:
        return self.adb("shell", sh(*args), serial=serial, timeout=timeout)

    def notifications(self, s: str, app: str = "", limit: int = 15) -> list[dict]:
        items = parse_notifications(self.shell(s, "dumpsys", "notification", "--noredact"))
        if app:
            items = [n for n in items if app.lower() in (n["app"] + " " + n["pkg"]).lower()]
        return items[:limit]

    def sms(self, s: str, sender: str = "", limit: int = 10) -> list[dict]:
        out = self.shell(s, "content", "query", "--uri", "content://sms/inbox",
                         "--projection", "address:date:read:body", "--sort", "date DESC")
        rows = parse_rows(out, ["address", "date", "read", "body"])
        if sender:
            rows = [r for r in rows if sender.lower() in r["address"].lower()]
        return [{"from": r["address"], "when": int(r["date"] or 0) // 1000, "unread": r["read"] == "0",
                 "text": r["body"][:500]} for r in rows[:limit]]

    def calls(self, s: str, limit: int = 10) -> list[dict]:
        out = self.shell(s, "content", "query", "--uri", "content://call_log/calls",
                         "--projection", "number:name:type:duration:date", "--sort", "date DESC")
        kinds = {"1": "incoming", "2": "outgoing", "3": "missed", "5": "rejected", "6": "blocked"}
        return [{"number": r["number"], "name": r["name"], "kind": kinds.get(r["type"], r["type"]),
                 "seconds": int(r["duration"] or 0), "when": int(r["date"] or 0) // 1000}
                for r in parse_rows(out, ["number", "name", "type", "duration", "date"])[:limit]]

    def contacts(self, s: str, name: str) -> list[dict]:
        out = self.shell(s, "content", "query", "--uri", "content://com.android.contacts/data/phones",
                         "--projection", "display_name:data1")
        return rank_contacts(parse_rows(out, ["display_name", "data1"]), name)[1][:10]

    def resolve(self, s: str, to: str) -> tuple[str, str]:
        """A phone number for `to`: a number, or a contact name that clearly means one person.
        A near miss (e.g. a speech-to-text typo) is suggested, never picked: the message must not go
        to someone the user didn't name."""
        to = to.strip()
        if NUMBER.match(to):
            return re.sub(r"[\s()-]", "", to), to
        out = self.shell(s, "content", "query", "--uri", "content://com.android.contacts/data/phones",
                         "--projection", "display_name:data1")
        tier, hits = rank_contacts(parse_rows(out, ["display_name", "data1"]), to)
        if tier and tier < 4 and len({h["number"] for h in hits}) == 1:
            return hits[0]["number"], hits[0]["name"]
        listing = "; ".join(f"{h['name']} {h['number']}" for h in hits[:6])
        if not hits:
            raise self.Error(f"no contact matches {to!r}; give a number")
        if tier == 4:
            raise self.Error(f"no contact called {to!r}; did you mean: {listing}? Ask the user which.")
        raise self.Error(f"{to!r} matches several numbers: {listing}. Ask the user which.")

    def press(self, s: str, labels: tuple[str, ...], wait: float = 2.5) -> bool:
        """Wait for the screen to settle, find a button by its label and tap it."""
        deadline = time.monotonic() + wait + 4
        time.sleep(wait)
        while time.monotonic() < deadline:
            self.shell(s, "uiautomator", "dump", "/sdcard/jarvis-ui.xml")
            xml = self.shell(s, "cat", "/sdcard/jarvis-ui.xml")
            if at := find_button(xml, labels):
                self.shell(s, "input", "tap", str(at[0]), str(at[1]))
                self.shell(s, "rm", "-f", "/sdcard/jarvis-ui.xml")
                return True
            time.sleep(1)
        self.shell(s, "rm", "-f", "/sdcard/jarvis-ui.xml")
        return False

    def awake(self, s: str) -> None:
        state = self.shell(s, "dumpsys", "window", "policy")
        if re.search(r"(isStatusBarKeyguard|mShowingLockscreen|showing)=true", state):
            raise self.Error("the phone is locked: unlock it, then try again")
        self.shell(s, "input", "keyevent", "KEYCODE_WAKEUP")

    def send_sms(self, s: str, to: str, text: str) -> str:
        number, who = self.resolve(s, to)
        self.awake(s)
        self.shell(s, "am", "start", "-a", "android.intent.action.SENDTO", "-d", f"smsto:{number}",
                   "--es", "sms_body", text, "--ez", "exit_on_sent", "true")
        if not self.press(s, ("send sms", "send message", "send")):
            return f"Opened a message to {who} with the text filled in, but couldn't find Send: send it on the phone."
        return f"SMS sent to {who} ({number})."

    def send_whatsapp(self, s: str, to: str, text: str, business: bool = False) -> str:
        number, who = self.resolve(s, to)
        self.awake(s)
        pkg = "com.whatsapp.w4b" if business else "com.whatsapp"
        url = f"https://api.whatsapp.com/send?phone={number.lstrip('+')}&text={quote(text)}"
        self.shell(s, "am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", pkg)
        if not self.press(s, ("send",), wait=3.5):
            return f"Opened WhatsApp to {who} with the text filled in, but couldn't find Send: send it on the phone."
        return f"WhatsApp message sent to {who} ({number})."

    def call(self, s: str, to: str) -> str:
        number, who = self.resolve(s, to)
        self.awake(s)
        self.shell(s, "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{number}")
        return f"Calling {who} ({number})."

    def call_key(self, s: str, what: str) -> str:
        key = {"answer": "KEYCODE_CALL", "end": "KEYCODE_ENDCALL"}[what]
        self.shell(s, "input", "keyevent", key)
        return "Answered." if what == "answer" else "Call ended."

    def call_state(self, s: str) -> dict:
        reg = self.shell(s, "dumpsys", "telephony.registry")
        state = re.search(r"mCallState=(\d)", reg)
        number = re.search(r"mCallIncomingNumber=(\S*)", reg)
        return {"state": {"0": "idle", "1": "ringing", "2": "in_call"}.get(state[1] if state else "0", "idle"),
                "number": number[1] if number else ""}
