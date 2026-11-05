"""Clock times and days in plain words ("10 am tomorrow", "18:30", "noon on Friday"), worked out by code. A 2B
model asked for a timer "at 10 AM tomorrow" set one for 43200 s that ended at 06:24 and called it 10 AM sharp."""

import re
from datetime import date, datetime, timedelta

_AMPM = r"(a\.?m\.?|p\.?m\.?)"
CLOCK = re.compile(rf"\b(?:(\d{{1,2}}):(\d{{2}})\s*{_AMPM}?|(\d{{1,2}})\s*{_AMPM}|(noon|midday|midnight))(?![\w:])", re.I)
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|"
                   r"nov|dec)[a-z]*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                   re.I)
DAILY = re.compile(r"\b(?:every ?day|daily|each day|every (?:morning|evening|night|afternoon))\b", re.I)
WEEKDAYS = re.compile(r"\b(?:every weekday|weekdays|on weekdays|each weekday|monday to friday)\b", re.I)
WEEKLY = re.compile(rf"\b(?:every|each) ({'|'.join(DAYS)})\b|\bweekly\b", re.I)


_DAY_WORDS = re.compile(rf"\b(?:day after tomorrow|tomorrow|today|tonight|(?:next |this |on )?(?:{'|'.join(DAYS)})|"
                        r"\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                        r"[a-z]*)\b", re.I)


def day_words(text: str) -> str:
    """The words in text that name a day ("tomorrow", "on Friday", "14 Nov"), or ""."""
    m = _DAY_WORDS.search(text or "")
    return m[0] if m else ""


def repeat_of(text: str) -> tuple[str, str]:
    """(repeat, weekday) the words ask for: ("daily", ""), ("weekdays", ""), ("weekly", "monday"), or ("", "")."""
    if WEEKDAYS.search(text or ""):
        return "weekdays", ""
    if m := WEEKLY.search(text or ""):
        return "weekly", (m[1] or "").lower()
    if DAILY.search(text or ""):
        return "daily", ""
    return "", ""


def clock(text: str, bare_hour: bool = False) -> tuple[int, int] | None:
    """(hour, minute) of the first clock time in text; a bare "10" counts only when bare_hour (a tool's own
    time argument), and then means the next 10 o'clock that is still to come is left to day()."""
    m = CLOCK.search(text or "")
    if m:
        if m[6]:
            return (12, 0) if m[6].lower() in ("noon", "midday") else (0, 0)
        hour, minute, ampm = (int(m[1]), int(m[2]), m[3]) if m[1] else (int(m[4]), 0, m[5])
    elif bare_hour and (b := re.fullmatch(r"\s*(\d{1,2})(?:\s*o'?clock)?\s*", text or "")):
        hour, minute, ampm = int(b[1]), 0, None
    else:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower().startswith("p") else 0)
    return (hour, minute) if hour < 24 and minute < 60 else None


def day(text: str, today: date) -> date | None:
    """The day text names: today, tomorrow, a weekday (the next one; today counts), or a date; None if none."""
    t = (text or "").lower()
    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if "tomorrow" in t:
        return today + timedelta(days=1)
    if re.search(r"\btoday|tonight\b", t):
        return today
    if m := _DATE.search(t):
        try:
            if m[1]:
                return date(int(m[1]), int(m[2]), int(m[3]))
            d, mon = (int(m[4]), m[5]) if m[4] else (int(m[7]), m[6])
            found = date(today.year, _MONTHS.index(mon[:3]) + 1, d)
            return found if found >= today else found.replace(year=today.year + 1)
        except ValueError:
            return None
    for i, name in enumerate(DAYS):
        if re.search(rf"\b{name}\b", t):
            return today + timedelta(days=(i - today.weekday()) % 7)
    return None


def when(time_text: str, day_text: str = "", now: datetime | None = None, bare_hour: bool = True) -> datetime | None:
    """The moment a time and day in words mean: the day named (or today), at the clock time; with no day named,
    a time already past today means tomorrow. None when there is no clock time."""
    now = now or datetime.now()
    hm = clock(time_text, bare_hour) or (clock(day_text) if day_text else None)
    if hm is None:
        return None
    on = day(f"{day_text} {time_text}", now.date())
    at = datetime.combine(on or now.date(), datetime.min.time()).replace(hour=hm[0], minute=hm[1])
    if on is None and at <= now:
        at += timedelta(days=1)
    return at


def spoken(at: datetime, now: datetime | None = None) -> str:
    """"today at 10:00", "tomorrow at 10:00", "on Friday at 10:00", "on 14 November at 10:00"."""
    now = now or datetime.now()
    days = (at.date() - now.date()).days
    on = "today" if days == 0 else "tomorrow" if days == 1 else f"on {at:%A}" if 1 < days < 7 else \
        f"on {at.day} {at:%B}"
    return f"{on} at {at:%H:%M}"
