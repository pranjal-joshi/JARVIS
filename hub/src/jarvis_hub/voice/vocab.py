"""Names Whisper tends to mishear ("Chico" for Chiku), put right after transcription.

The names come from the knowledge base (page titles, and one name per block on the page
jarvis/vocabulary) and from people JARVIS recently texted, called or looked up. Only a word that
Whisper wrote as a proper noun is changed, only to a name starting with the same letter and of
about the same length, so ordinary speech is left alone."""

import difflib
import json
import re
import time

from jarvis_hub.audit.store import AuditStore
from jarvis_hub.kb.graph import ObsidianVault

VOCAB_PAGE = "jarvis/vocabulary"
PEOPLE_TOOLS = ("adb_sms_send", "adb_whatsapp_send", "adb_call", "adb_contacts")
CUTOFF = 0.6
_WORD = re.compile(r"\b[A-Z][a-z']{2,}\b")
# words Whisper capitalises that are never someone's name
_COMMON = set("""I I'm I'll I've Hey Hello Hi Okay Yes No The What When Where Who Why How Can Could
Would Should Will Shall Please Thanks Thank Set Turn Send Text Call Read Tell Open Play Stop Start
What's Jarvis Sir Monday Tuesday Wednesday Thursday Friday Saturday Sunday Today Tomorrow""".split())


class Vocabulary:
    def __init__(self, graph: ObsidianVault, audit: AuditStore, ttl_s: float = 60) -> None:
        self.graph, self.audit, self.ttl_s = graph, audit, ttl_s
        self._names: list[str] = []
        self._at = 0.0

    def names(self) -> list[str]:
        if time.time() - self._at < self.ttl_s:
            return self._names
        names: set[str] = set()
        for page in self.graph.pages():
            if not page.is_journal:
                last = page.name.split("/")[-1].strip()
                if 0 < len(last.split()) <= 3:
                    names.add(last)
        if page := self.graph.page(VOCAB_PAGE):
            names |= {b.text.strip() for b in page.blocks if 0 < len(b.text.split()) <= 3}
        for row in self.audit.recent(200):
            if row.get("tool") in PEOPLE_TOOLS:
                try:
                    args = json.loads(row.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = row.get("arguments") if isinstance(row.get("arguments"), dict) else {}
                for k in ("to", "name", "query"):
                    v = str(args.get(k) or "").strip()
                    if v and not v.replace(" ", "").replace("+", "").isdigit():
                        names.add(v)
        self._names = sorted(n for n in names if n[:1].isalpha())
        self._at = time.time()
        return self._names

    def correct(self, text: str) -> str:
        words = {w.lower(): w for n in self.names() for w in n.split() if len(w) >= 3 and w[0].isupper()}
        if not words:
            return text

        def fix(m: re.Match) -> str:
            heard = m[0]
            if heard.lower() in words or heard in _COMMON:
                return heard
            alike = [w for w in words if w[0] == heard[0].lower() and abs(len(w) - len(heard)) <= 2]
            near = difflib.get_close_matches(heard.lower(), alike, 1, CUTOFF)
            return words[near[0]] if near else heard
        return _WORD.sub(fix, text)
