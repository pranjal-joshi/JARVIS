"""Turn an on-screen reply (possibly markdown) into something worth saying aloud.

The HUD shows the full reply; speech gets the prose, without markup, code or tables.
Replies are meant to be short already (the persona caps them); the sentence cap here
is only a quiet safety net for a runaway answer."""

import re

MAX_SPOKEN = 500

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_SIGNATURE = re.compile(r"^\s*— .*$", re.M)  # ask_claude/ask_opencode footer: "— claude · model (9 s)"
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKS = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),           # headings
    (re.compile(r"^\s*>\s?", re.M), ""),                     # quotes
    (re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M), ""),     # list markers
    (re.compile(r"`([^`]*)`"), r"\1"),                       # inline code
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),                # bold
    (re.compile(r"(?<![\w*])[*_](\S.*?)[*_](?![\w*])"), r"\1"),  # italics
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"^\s*([-*_]\s*){3,}$", re.M), ""),          # rules
]
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

# Machine strings nobody wants read out letter by letter. Shown on screen, never spoken.
# Ordinary numbers (amounts, times, dates, short codes) are kept.
_JARGON = [
    (re.compile(r"\b(?:https?://|www\.)\S+?(?=[.,;:!?)]?(?:\s|$))", re.I), "the link"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]*\w"), "the email address"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), ""),  # GUIDs
    (re.compile(r"(?:\b[A-Za-z]:[\\/]|~/|(?<![\w:])/)[\w.~-]+(?:[\\/][\w.~-]+)+"), "the path"),  # file paths
    (re.compile(r"\b(?:com|org|net|in|io)\.[a-z0-9_]+(?:\.[a-z0-9_]+)+\b"), ""),  # app ids (com.whatsapp.w4b)
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), ""),  # hashes, keys
    # serials, refs and tokens: 8+ characters mixing letters and digits (61271XEBF1YAF1, sk_live_ab12...)
    (re.compile(r"\b(?=\w*[A-Za-z])(?=\w*\d)\w{8,}\b"), ""),
    (re.compile(r"\b[A-Z0-9]{3,}(?:-[A-Z0-9]{3,}){2,}\b"), ""),  # dashed keys (ABCD-12EF-9XYZ); dates stay
    (re.compile(r"\b\d{11,}\b"), ""),  # long reference numbers (UPI refs, account numbers)
]


def _drop_jargon(text: str) -> str:
    for rx, rep in _JARGON:
        text = rx.sub(rep, text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)  # brackets left empty
    for _ in range(2):  # tidy what's left: "and id ." / "serial , ref ," -> nothing
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"(?:\b(?:and|or|with|at|id|serial|ref|key|token|app|number|code)\s*)+(?=[,.;:!?]|$)", "",
                      text, flags=re.I)
        text = re.sub(r"[,;:](?=\s*[,.;:!?]|\s*$)", "", text)
    text = re.sub(r"(?<!\.)\.\s*\.(?!\.)", ".", text)  # "123456. ." -> "123456." (an ellipsis stays)
    return re.sub(r"\s{2,}", " ", text).strip()


def to_speech(text: str, max_chars: int = MAX_SPOKEN) -> str:
    """Spoken form of a reply; "" when nothing in it is worth saying (e.g. only code)."""
    out = _FENCE.sub(" ", text)
    out = _TABLE_ROW.sub("", out)
    out = _SIGNATURE.sub("", out)
    out = _LINK.sub(r"\1", out)
    out = _drop_jargon(out)
    for rx, rep in _MARKS:
        out = rx.sub(rep, out)
    # each remaining line reads as its own sentence
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    out = " ".join(ln if ln[-1] in ".!?:;," else ln + "." for ln in lines)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > max_chars:
        spoken = ""
        for s in _SENTENCE.split(out):
            if spoken and len(spoken) + len(s) > max_chars:
                break
            spoken = f"{spoken} {s}".strip()
        out = spoken[:max_chars]
    if out.endswith(":"):
        out = out[:-1] + "."
    return out


# ---------- listening, not reading (voice satellites) ----------
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
           "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December"]
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(\s*[ap]\.?m\b\.?)?", re.I)
_ISO_DATE = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_DAY_MONTH_YEAR = re.compile(rf"\b(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?),?\s+20\d\d\b", re.I)
_MONTH_DAY_YEAR = re.compile(rf"\b((?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?),?\s+20\d\d\b", re.I)
_BIG = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+)(?:\.\d+)?(?!\d|,\d)")
_SYMBOLS = [(re.compile(r"\s*%"), " percent"), (re.compile(r"\s*°\s*C\b"), " degrees"),
            (re.compile(r"\s*°"), " degrees"), (re.compile(r"\s+&\s+"), " and "),
            (re.compile(r"₹\s?([\d,.]+)"), r"\1 rupees"), (re.compile(r"£\s?([\d,.]+)"), r"\1 pounds"),
            (re.compile(r"\$\s?([\d,.]+)"), r"\1 dollars"), (re.compile(r"€\s?([\d,.]+)"), r"\1 euros")]


def _spoken_time(m: re.Match) -> str:
    h, mins = int(m[1]), int(m[2])
    if m[3]:  # "5:15 pm": the hour is already on the 12-hour clock
        h = h % 12 + (12 if m[3].strip().lower().startswith("p") else 0)
    to = mins > 30
    hour = (h + to) % 12 or 12
    if mins == 0:
        said = f"{hour} o'clock"
    elif mins in (15, 30, 45):
        said = {15: "quarter past", 30: "half past", 45: "quarter to"}[mins] + f" {hour}"
    else:
        said = f"{60 - mins if to else mins} {'to' if to else 'past'} {hour}"
    hh = (h + to) % 24
    part = "in the morning" if 5 <= hh < 12 else "in the afternoon" if 12 <= hh < 17 else \
        "in the evening" if 17 <= hh < 21 else "at night"
    return f"{said} {part}"


def _round_big(m: re.Match) -> str:
    n = int(m[1].replace(",", ""))
    if n < 10_000:
        return m[1].replace(",", "")
    if n >= 1_000_000:
        return f"about {round(n / 1_000_000, 1):g} million"
    return f"about {round(n / 1000):g} thousand"


def for_ear(text: str) -> str:
    """Satellite style: times as spoken ("quarter past five in the evening"), dates without the
    year, big numbers rounded, symbols as words. Applied to speech only, after to_speech."""
    text = _ISO_DATE.sub(lambda m: f"{int(m[3])} {_MONTH_NAMES[int(m[2]) - 1]}", text)
    text = _DAY_MONTH_YEAR.sub(r"\1", text)
    text = _MONTH_DAY_YEAR.sub(r"\1", text)
    text = _TIME.sub(_spoken_time, text)
    text = _BIG.sub(_round_big, text)
    for rx, rep in _SYMBOLS:
        text = rx.sub(rep, text)
    return re.sub(r"\s{2,}", " ", text).strip()
