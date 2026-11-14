"""Phone routines: what the user does by hand on the phone, recorded as plain steps and replayed later without
a model; and getting a stuck screen moving again (a system pop-up in the way).

Steps are one per line, as a skill page keeps them (skills/<name>.md, replay: phone):
    open com.frameo.app
    tap 540 1210
    swipe 540 1600 540 500 300ms
    key back
    wait 1.5s
Recording reads the touchscreen's raw events (`adb shell getevent -lt`) and scales them to screen pixels, so a
routine replays on the phone it was recorded on, in the same orientation."""

import re
import xml.etree.ElementTree as ET

KEY_NAMES = {"KEY_BACK": "back", "KEY_HOMEPAGE": "home", "KEY_HOME": "home", "KEY_VOLUMEUP": "volume_up",
             "KEY_VOLUMEDOWN": "volume_down", "KEY_PLAYPAUSE": "play_pause", "KEY_POWER": "sleep"}
MOVE_PX = 40  # a touch that travels further than this is a swipe, not a tap
MIN_WAIT, MAX_WAIT = 0.4, 5.0  # seconds between steps when replayed
MAX_STEPS = 200
EVENT = re.compile(r"\[\s*([\d.]+)\]\s+(\S+):\s+(EV_\w+)\s+(\S+)\s+(\S+)")


def touch_range(getevent_lp: str) -> tuple[str, int, int] | None:
    """(touch device, max x, max y) from `getevent -lp`: the device that reports ABS_MT_POSITION_X."""
    for block in re.split(r"(?m)^add device \d+: ", getevent_lp)[1:]:
        dev = block.split("\n", 1)[0].strip()
        mx = re.search(r"ABS_MT_POSITION_X\s*:.*?max (\d+)", block)
        my = re.search(r"ABS_MT_POSITION_Y\s*:.*?max (\d+)", block)
        if mx and my:
            return dev, int(mx[1]), int(my[1])
    return None


def parse_getevent(lines: list[str], touch: tuple[str, int, int], size: tuple[int, int]) -> list[str]:
    """Raw `getevent -lt` lines -> steps. touch: (device, max x, max y); size: the screen (width, height)."""
    dev, max_x, max_y = touch
    w, h = size
    steps, last_t = [], None
    touching, start = False, None  # start: (t, x, y) of the finger that is down
    x = y = None  # multitouch protocol B only reports what changed, so the last position carries over

    for line in lines:
        m = EVENT.search(line)
        if not m:
            continue
        t, where, kind, code, value = float(m[1]), m[2], m[3], m[4], m[5]
        at = t
        if kind == "EV_KEY" and code in KEY_NAMES and value == "DOWN":
            word = f"key {KEY_NAMES[code]}"
        elif where != dev:
            continue
        elif code == "ABS_MT_POSITION_X":
            x = round(int(value, 16) * w / (max_x + 1))
            continue
        elif code == "ABS_MT_POSITION_Y":
            y = round(int(value, 16) * h / (max_y + 1))
            continue
        elif code == "ABS_MT_TRACKING_ID":
            if value.lower() != "ffffffff":
                touching, start = True, None
                continue
            touching, began = False, start
            start = None
            if not began or x is None:
                continue
            at, x0, y0 = began
            word = (f"swipe {x0} {y0} {x} {y} {max(100, round((t - at) * 1000))}ms"
                    if abs(x - x0) + abs(y - y0) > MOVE_PX else f"tap {x0} {y0}")
        elif code == "SYN_REPORT" and touching and start is None and x is not None and y is not None:
            start = (t, x, y)  # the first full frame after the finger went down: where it landed
            continue
        else:
            continue
        if last_t is not None and at - last_t >= MIN_WAIT:
            steps.append(f"wait {min(at - last_t, MAX_WAIT):.1f}s")
        steps.append(word)
        last_t = t
    return steps[:MAX_STEPS]


STEP = re.compile(r"^(?:(tap) (\d+) (\d+)|(swipe) (\d+) (\d+) (\d+) (\d+)(?: (\d+)ms)?|(key) ([a-z_]+)|"
                  r"(wait) ([\d.]+)s?|(open) ([\w.]+))$", re.I)


def parse_steps(lines: list[str]) -> list[tuple]:
    """A skill page's step lines -> (kind, *args). Anything else on the page is refused, so a routine only ever
    taps, swipes, presses keys, waits and opens apps."""
    out = []
    for raw in lines:
        line = re.sub(r"^\s*[-*]\s*", "", raw).strip()
        if not line:
            continue
        m = STEP.match(line)
        if not m:
            raise ValueError(f"not a routine step: {line!r}")
        g = [v for v in m.groups() if v is not None]
        kind = g[0].lower()
        if kind in ("tap", "swipe"):
            out.append((kind, *[int(v) for v in g[1:]]))
        elif kind == "wait":
            out.append(("wait", min(float(g[1]), MAX_WAIT)))
        else:
            out.append((kind, g[1]))
    if len(out) > MAX_STEPS:
        raise ValueError(f"a routine has at most {MAX_STEPS} steps")
    return out


# A system pop-up in the way, and the one button that gets rid of it safely. Only these buttons are ever
# pressed: never Allow, OK, Accept or anything that grants or buys something.
POPUPS = [
    (r"isn't responding|is not responding", ("Wait",)),
    (r"keeps stopping|has stopped", ("Close app",)),
    (r"battery optimi[sz]ation|run in (?:the )?background|ignore battery", ("Deny", "Don't allow", "Not now", "Cancel")),
    (r"rate (?:this app|us)|enjoying|leave a review", ("Not now", "No thanks", "Later", "Maybe later")),
    (r"update (?:available|required)|new version", ("Not now", "Later", "Skip", "No thanks")),
]


def find_popup(ui_xml: str) -> tuple[str, int, int] | None:
    """(button text, x, y) of the safe button on a known pop-up in a `uiautomator dump`, or None."""
    try:
        nodes = list(ET.fromstring(ui_xml).iter("node"))
    except ET.ParseError:
        return None
    texts = " ".join(n.get("text", "") + " " + n.get("content-desc", "") for n in nodes).lower()
    for pattern, buttons in POPUPS:
        if not re.search(pattern, texts):
            continue
        for want in buttons:
            for n in nodes:
                label = (n.get("text") or n.get("content-desc") or "").strip()
                b = re.findall(r"\d+", n.get("bounds", ""))
                if label.lower() == want.lower() and len(b) == 4:
                    x1, y1, x2, y2 = map(int, b)
                    return label, (x1 + x2) // 2, (y1 + y2) // 2
    return None
