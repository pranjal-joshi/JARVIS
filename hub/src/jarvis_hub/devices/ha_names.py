"""Home Assistant device names for live enums on its Assist tools (HassTurnOn, HassLightSet...).

The names come from Home Assistant's own GetLiveContext tool, read through the caller's policy check and kept in the
audit like any call, at most once a minute. They are Home Assistant's data, so they count as outside text: a turn
offered them is marked as having read Home Assistant's data, and a change naming a device the user didn't say asks
first (guard/hooks.py after_outside_text). Only short plain names are offered; read-only domains are left out."""

import re
import time
from collections.abc import Awaitable, Callable

_ITEM = re.compile(r"^-\s+names:\s*(.+?)\s*$((?:\n[ \t]+\S.*$)*)", re.M)
_DOMAIN = re.compile(r"^\s+domain:\s*['\"]?([\w.]+)", re.M)
_PLAIN = re.compile(r"^[\w][\w .'&()/+-]{0,39}$")
READ_ONLY = {"sensor", "binary_sensor", "weather", "sun", "person", "zone", "calendar", "todo", "event",
             "device_tracker", "conversation", "update", "image", "camera"}
# Assist tools that act on one kind of device; the others (HassTurnOn/Off) take any that can change
DOMAINS = {"HassLightSet": {"light"}, "HassFan": {"fan"}, "HassClimate": {"climate"}, "HassMedia": {"media_player"},
           "HassSetVolume": {"media_player"}, "HassVacuum": {"vacuum"}, "HassLawnMower": {"lawn_mower"},
           "HassCover": {"cover"}, "HassValve": {"valve"}}
OUTSIDE = "Home Assistant's device names"


def parse(context: str) -> list[tuple[str, str]]:
    """(first name, domain) of each device in GetLiveContext's text."""
    out = []
    for m in _ITEM.finditer(context or ""):
        name = m[1].split(",")[0].strip().strip("'\"")
        domain = (_DOMAIN.search(m[2]) or [None, ""])[1]
        if _PLAIN.match(name) and len(name.split()) <= 5:  # a name, not a sentence someone typed in
            out.append((name, domain))
    return out


class HANames:
    def __init__(self, read: Callable[[object, object], Awaitable[str | None]], ttl_s: float = 60) -> None:
        """read(tool, turn): the context tool's text after the caller's policy check and audit, or None."""
        self._read, self.ttl_s = read, ttl_s
        self._devices: list[tuple[str, str]] = []
        self._at = 0.0

    async def names(self, context_tool, assist: str, turn) -> list[str]:
        """The names an Assist tool (e.g. HassTurnOn) can act on now."""
        if time.time() - self._at > self.ttl_s:
            text = await self._read(context_tool, turn)
            self._at = time.time()
            if text is not None:
                self._devices = parse(text)
        wanted = next((d for k, d in DOMAINS.items() if assist.startswith(k)), None)
        return [n for n, d in self._devices if (d in wanted if wanted else d not in READ_ONLY)]
