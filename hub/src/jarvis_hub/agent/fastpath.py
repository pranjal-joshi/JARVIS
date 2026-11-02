"""Fast paths: requests with a closed vocabulary answered by code, with no model call.

A 2B model needs 4-8 s per call on the MX550 and gets date arithmetic wrong; "what time is it" or
"set a timer for 10 minutes" needs neither. Each matched request becomes a short plan of tool calls,
run through the normal approval gate and audit, and a templated JARVIS reply. Anything that doesn't
match exactly (or whose tool errors) falls through to the model, so a miss only costs the old path."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from jarvis_hub.kb.graph import LogseqGraph

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
       r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "sixty": 60,
        "half an": 0.5}
_COUNT = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|" \
         r"forty-five|sixty|half an)"
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
HOME_WORDS = r"(?:where i live|at home|my (?:home|city|place)|here|outside)"
NAMED_DAYS = {"christmas": (12, 25), "christmas day": (12, 25), "new year": (1, 1), "new year's day": (1, 1),
              "new years": (1, 1), "valentine's day": (2, 14), "halloween": (10, 31)}


@dataclass
class Plan:
    intent: str
    steps: list[tuple[str, dict]] = field(default_factory=list)  # (internal tool name, args)
    say: Callable[[list[str]], str] = lambda results: ""  # tool outputs -> the reply


def _clean(text: str) -> str:
    text = re.sub(r"^\W*(?:hey |ok |okay )?(?:jarvis\W+)?(?:please |could you |can you |would you )?", "",
                  text.strip(), flags=re.I)
    return re.sub(r"[\s?.!]+$", "", re.sub(r"\s+(?:please|for me|jarvis)\W*$", "", text, flags=re.I)).lower()


def _count(word: str) -> float:
    return float(word) if word[0].isdigit() else _NUM[word]


def _seconds(n: str, unit: str) -> float:
    return _count(n) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)


def _span(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        h = int(seconds // 3600)
        return f"{h} hour{'s' * (h != 1)}"
    if seconds >= 60 and seconds % 60 == 0:
        m = int(seconds // 60)
        return f"{m} minute{'s' * (m != 1)}"
    return f"{int(seconds)} seconds"


def _body(result: str) -> str:
    """A tool's output without the fence and the untrusted-data preamble."""
    body = result.split("\n", 1)[1].rsplit("\n</tool_result>", 1)[0] if result.startswith("<tool_result") else result
    lines = body.splitlines()
    return "\n".join(lines[1:] if lines and lines[0].startswith("The following is") else lines).strip()


def _target_date(spec: str, today: date) -> date | None:
    spec = spec.strip().removeprefix("the ")
    if spec in NAMED_DAYS:
        month, day = NAMED_DAYS[spec]
    else:
        m = re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? {_MON}", spec) or \
            re.fullmatch(rf"{_MON} (\d{{1,2}})(?:st|nd|rd|th)?", spec)
        if not m:
            return None
        d, mon = (m[1], m[2]) if m[1][0].isdigit() else (m[2], m[1])
        month = next(i for i, name in enumerate(MONTHS, 1) if name.startswith(mon[:3]))
        day = int(d)
    try:
        target = date(today.year, month, day)
        return target if target >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


_WORD_OPS = [(r"\bmultiplied by\b|\btimes\b|\bx\b|×", "*"), (r"\bdivided by\b|\bover\b|÷", "/"),
             (r"\bplus\b|\badded to\b", "+"), (r"\bminus\b|\btake away\b", "-"), (r"\bsquared\b", "**2"),
             (r"\bcubed\b", "**3"), (r"\bto the power of\b", "**")]


def _expression(text: str) -> str | None:
    m = re.fullmatch(r"(?:what(?:'s| is)|calculate|compute|work out|how much is)\s+(.+)", text)
    if not m:
        return None
    e = m[1].replace(",", "")
    e = re.sub(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)\s+of\s+(\d+(?:\.\d+)?)", r"(\1/100*\2)", e)
    e = re.sub(r"(?:the )?square root of (\d+(?:\.\d+)?)", r"sqrt(\1)", e)
    for rx, op in _WORD_OPS:
        e = re.sub(rx, op, e)
    e = e.strip()
    if not re.fullmatch(r"[\d\s.+\-*/()]*(?:sqrt\([\d.]+\))?[\d\s.+\-*/()]*", e) or not re.search(r"\d.*[-+*/]|sqrt", e):
        return None
    return e


class FastPaths:
    def __init__(self, graph: LogseqGraph, home_place: str = "") -> None:
        self.graph, self.home_place = graph, home_place

    def home(self) -> str:
        """Where the user lives: HOME_PLACE, else the first place named on the jarvis/location page."""
        if self.home_place:
            return self.home_place
        page = self.graph.page("jarvis/location")
        for b in page.blocks if page else []:
            if m := re.search(r"\b(?:in|at|from)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)", b.text):
                return m[1]
        return ""

    def match(self, utterance: str, today: date | None = None) -> Plan | None:
        text = _clean(utterance)
        if not text or len(text) > 90 or re.search(r"\b(and|then|also)\b", text):
            return None  # several requests in one: the model handles those
        today = today or date.today()
        for fn in (self._time, self._days_until, self._maths, self._weather, self._timers, self._tasks,
                   self._help):
            if plan := fn(text, today):
                return plan
        return None

    # ---------- intents ----------
    def _time(self, text: str, today: date) -> Plan | None:
        if re.fullmatch(r"(?:what(?:'s| is) the )?(?:date|day)(?: is it)?(?: today)?|what day is (?:it|today)"
                        r"|what(?:'s| is) (?:the date|today's date|today)", text):
            return Plan("date", [("get_time", {})],
                        lambda r: f"It's {_when(r[0]):%A} {_when(r[0]).day} {_when(r[0]):%B}, sir.")
        m = re.fullmatch(r"(?:what(?:'s| is) the )?time(?: is it)?(?: now| right now)?(?: in ([a-z][a-z .'-]+))?"
                         r"|what time is it(?: now)?(?: in ([a-z][a-z .'-]+))?", text)
        if not m:
            return None
        place = (m[1] or m[2] or "").strip()
        return Plan("time", [("get_time", {"timezone": place} if place else {})],
                    lambda r: f"It's {_when(r[0]):%H:%M}" + (f" in {place.title()}" if place else "") + ", sir.")

    def _days_until(self, text: str, today: date) -> Plan | None:
        m = re.fullmatch(r"how (?:many days|long) (?:is it |are there )?(?:until|till|til|to|before) (.+)", text)
        target = _target_date(m[1], today) if m else None
        if not target:
            return None
        n = (target - today).days
        if n == 0:
            return Plan("days_until", say=lambda r: "That's today, sir.")
        return Plan("days_until", say=lambda r: f"{n} day{'s' * (n != 1)}, sir, until {target:%A} "
                                                 f"{target.day} {target:%B}" + (f" {target.year}" if target.year != today.year else "") + ".")

    def _maths(self, text: str, today: date) -> Plan | None:
        expr = _expression(text)
        return Plan("maths", [("calculate", {"expression": expr})], lambda r: _body(r[0])) if expr else None

    def _weather(self, text: str, today: date) -> Plan | None:
        if not re.search(r"\b(weather|forecast|temperature|rain|raining|sunny|hot|cold|umbrella)\b", text):
            return None
        m = re.search(r"\b(?:in|at|for) ([a-z][a-z .'-]{1,40}?)(?: (?:today|tomorrow|now|this week|on \w+day))?$", text)
        place = m[1].strip() if m else ""
        if re.search(HOME_WORDS, text) or place in ("home", "here", "today", "tomorrow"):
            place = ""
        place = place or self.home()  # no place named: where the user lives
        if not place:
            return None
        day = "Tomorrow" if "tomorrow" in text else next(
            (d.title() for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
             if d in text), "Today")
        return Plan("weather", [("weather", {"place": place})], lambda r: _weather_reply(_body(r[0]), day, text))

    def _timers(self, text: str, today: date) -> Plan | None:
        m = re.fullmatch(rf"(?:set|start|put on) (?:a |an )?(?:(.+?) )?timer (?:for |of )?{_COUNT} {_UNIT}"
                         rf"(?: (?:for|called|named) (?:the )?(.+?))?", text) or \
            re.fullmatch(rf"(?:set|start) (?:a |an )?{_COUNT} {_UNIT} timer(?: (?:for|called|named) (?:the )?(.+?))?",
                         text)
        if m:
            groups = m.groups()
            if len(groups) == 4:
                label, n, unit, label2 = groups
            else:
                n, unit, label2 = groups
                label = None
            secs, label = _seconds(n, unit), (label or label2 or "timer").strip()
            return Plan("timer_set", [("set_timer", {"seconds": secs, "label": label})],
                        lambda r: f"{'Timer' if label == 'timer' else label.capitalize() + ' timer'} set for "
                                  f"{_span(secs)}, sir.")
        m = re.fullmatch(rf"remind me in {_COUNT} {_UNIT} to (.+)", text) or \
            re.fullmatch(rf"remind me to (.+) in {_COUNT} {_UNIT}", text)
        if m:
            what, n, unit = (m[3], m[1], m[2]) if m[1][0].isdigit() or m[1] in _NUM else (m[1], m[2], m[3])
            secs = _seconds(n, unit)
            return Plan("remind", [("set_timer", {"seconds": secs, "label": what})],
                        lambda r: f"I'll remind you to {what} in {_span(secs)}, sir.")
        if re.fullmatch(r"(?:what|which|any|how many) timers?(?: are)?(?: (?:running|set|on|left))?"
                        r"|(?:list|show) (?:my |the )?timers|how long (?:is )?left on (?:my |the )?timers?", text):
            return Plan("timer_list", [("list_timers", {})], lambda r: _timers_reply(_body(r[0])))
        m = re.fullmatch(r"(?:cancel|stop|delete) (?:the |my )?(.+?) timer|(?:cancel|stop) (?:the |my )?timer", text)
        if m:
            label = (m[1] or "").strip()
            return Plan("timer_cancel", [("cancel_timer", {"label": label})],
                        lambda r: f"{(label or 'The timer').capitalize()}{' timer' if label else ''} cancelled, sir.")
        return None

    def _tasks(self, text: str, today: date) -> Plan | None:
        if not re.fullmatch(r"(?:what(?:'s| is| are) )?(?:on )?my (?:open |current )?(?:tasks|to-?dos?|to-?do list)"
                            r"(?: list)?(?: today)?|(?:list|read|show) (?:me )?my (?:tasks|to-?dos?|to-?do list)"
                            r"|what do i (?:have to|need to) do(?: today)?", text):
            return None
        return Plan("tasks", [("kb_tasks", {})], lambda r: _tasks_reply(_body(r[0])))

    def _help(self, text: str, today: date) -> Plan | None:
        if not re.fullmatch(r"what can you do|what are you able to do|help|what do you do|how can you help(?: me)?",
                            text):
            return None
        return Plan("help", say=lambda r: (
            "I can tell you the time, weather and dates, do sums, set timers and reminders, keep and search your "
            "notes and tasks, check your laptop and phone, read your messages, and search the web. "
            "Say \"ask Claude\" for anything harder, sir."))


def _when(result: str) -> datetime:
    """The datetime in get_time's output ("Monday 02 November 2026, 11:40 (IST)")."""
    return datetime.strptime(_body(result).split(" (")[0], "%A %d %B %Y, %H:%M")


def _weather_reply(body: str, day: str, asked: str) -> str:
    lines = body.splitlines()
    now = lines[0] if lines else ""
    days = {ln.split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
    rain = re.search(r"\b(rain|raining|umbrella)\b", asked)
    if day == "Today" and not rain:
        today = days.get("Today", "")
        return f"{now} Today: {today}" if today else now
    forecast = days.get(day)
    if not forecast:
        return now
    if rain and (m := re.search(r"(\d+)% chance of rain", forecast)):
        chance = int(m[1])
        verdict = "Rain is likely" if chance >= 60 else "Rain is possible" if chance >= 30 else "Rain is unlikely"
        return f"{verdict} {day.lower() if day in ('Today', 'Tomorrow') else 'on ' + day}, sir: {forecast}"
    return f"{day}: {forecast}"


def _timers_reply(body: str) -> str:
    if body.startswith("No timers"):
        return "No timers are running, sir."
    parts = []
    for line in body.splitlines():
        label, _, left = line.rpartition(": ")
        if label and left.split()[0].isdigit():
            secs = int(left.split()[0])
            parts.append(f"{label}, {_span(round(secs / 60) * 60) if secs >= 90 else f'{secs} seconds'} left")
    return f"{len(parts)} timer{'s' * (len(parts) != 1)}: " + "; ".join(parts) + "."


def _tasks_reply(body: str) -> str:
    tasks = [re.sub(r"^(TODO|DOING|LATER|NOW|WAIT(?:ING)?)\s+", "", ln).rsplit(" [", 1)[0]
             for ln in body.splitlines() if re.match(r"(TODO|DOING|LATER|NOW|WAIT)", ln)]
    if not tasks:
        return "No open tasks, sir."
    top = "; ".join(tasks[:3])
    more = f", and {len(tasks) - 3} more" if len(tasks) > 3 else ""
    return f"You have {len(tasks)} open task{'s' * (len(tasks) != 1)}: {top}{more}."
