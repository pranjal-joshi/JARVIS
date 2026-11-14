"""Recording, replay steps and pop-ups, on sample adb output (run: cd hub && uv run pytest ../bridge)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from phone_routine import find_popup, parse_getevent, parse_steps, touch_range  # noqa: E402

LP = """add device 1: /dev/input/event1
  name:     "gpio-keys"
add device 2: /dev/input/event2
  name:     "touchscreen"
  events:
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 2159, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 4799, fuzz 0, flat 0, resolution 0
"""


def ev(t, dev, kind, code, value):
    return f"[ {t:12.6f}] {dev}: {kind:<12} {code:<20} {value}"


def touch(t, x, y, x2=None, y2=None, dt=0.08):
    """A finger down at (x, y) in raw units, maybe moving to (x2, y2), then up."""
    d = "/dev/input/event2"
    out = [ev(t, d, "EV_ABS", "ABS_MT_TRACKING_ID", "00000012"), ev(t, d, "EV_ABS", "ABS_MT_POSITION_X", f"{x:08x}"),
           ev(t, d, "EV_ABS", "ABS_MT_POSITION_Y", f"{y:08x}"), ev(t, d, "EV_SYN", "SYN_REPORT", "00000000")]
    if x2 is not None:
        out += [ev(t + dt / 2, d, "EV_ABS", "ABS_MT_POSITION_X", f"{x2:08x}"),
                ev(t + dt / 2, d, "EV_ABS", "ABS_MT_POSITION_Y", f"{y2:08x}"),
                ev(t + dt / 2, d, "EV_SYN", "SYN_REPORT", "00000000")]
    return out + [ev(t + dt, d, "EV_ABS", "ABS_MT_TRACKING_ID", "ffffffff"),
                  ev(t + dt, d, "EV_SYN", "SYN_REPORT", "00000000")]


def test_raw_touches_become_taps_swipes_keys_and_waits_in_screen_pixels():
    rng = touch_range(LP)
    assert rng == ("/dev/input/event2", 2159, 4799)
    lines = touch(100.0, 1080, 2400) + touch(102.0, 1080, 3600, 1080, 1200, dt=0.3) + \
        [ev(102.5, "/dev/input/event1", "EV_KEY", "KEY_BACK", "DOWN"),
         ev(102.6, "/dev/input/event1", "EV_KEY", "KEY_BACK", "UP")]
    steps = parse_getevent(lines, rng, (1080, 2400))  # raw is twice the screen's pixels
    assert steps == ["tap 540 1200", "wait 1.9s", "swipe 540 1800 540 600 300ms", "key back"]


def test_only_routine_steps_are_accepted():
    assert parse_steps(["- open com.frameo.app", "tap 540 1200", "wait 9s", "key back",
                        "swipe 1 2 3 4 300ms"]) == [("open", "com.frameo.app"), ("tap", 540, 1200), ("wait", 5.0),
                                                    ("key", "back"), ("swipe", 1, 2, 3, 4, 300)]
    with pytest.raises(ValueError):
        parse_steps(["tap 1 2", "rm -rf /sdcard"])


def ui(*nodes):
    return "<hierarchy>" + "".join(f'<node text="{t}" content-desc="" bounds="{b}"/>' for t, b in nodes) + "</hierarchy>"


def test_a_known_pop_up_gets_its_safe_button_and_nothing_else_is_pressed():
    anr = ui(("Settings isn't responding", "[0,0][10,10]"), ("Close app", "[0,100][200,200]"),
             ("Wait", "[0,300][200,400]"))
    assert find_popup(anr) == ("Wait", 100, 350)
    battery = ui(("Let app always run in background?", "[0,0][1,1]"), ("Allow", "[0,0][10,10]"),
                 ("Deny", "[20,20][40,40]"))
    assert find_popup(battery) == ("Deny", 30, 30)
    assert find_popup(ui(("Allow Frameo to access photos?", "[0,0][1,1]"), ("Allow", "[0,0][10,10]"))) is None
    assert find_popup("not xml") is None
