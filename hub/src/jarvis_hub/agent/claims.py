"""A reply that says something was remembered when nothing was written to the notes.

A 2B model often answers "Very good, sir, that's committed to memory." and calls no tool. Exact words miss
most of these, and neither embeddings nor asking the model itself tell them apart from honest replies
("I couldn't find that in your notes", "Shall I save it?"; measured on the live model), so this goes by the
shape of a claim: the speaker (I, I'll, that's, consider it...) and, within a few words, a memory word
(remember, noted, on record, in mind, hold on to, filed...), or a passive "is noted". Negations, questions
and offers, recalling what the user said before, and the memory word as a thing ("a good note") are not
claims."""

import re

_SPEAKER = (r"\b(?:let me|let's|I|I'?ll|I'?ve|I'?d|I have|I will|I shall|I'?m going to|I am going to|I'?m|I am|that'?s|that is|"
            r"it'?s|it is|this is|consider it|now|duly|all)\b")
_MEMORY = (r"\b(?:remember(?:ed)?|memori[sz]ed?|memory|note[ds]?|recorded|record|saved?|stored?|logged|log|"
           r"jot(?:ted)?|filed|file|kept|keep|committed|commit|in mind|on record|on file|in the book|"
           r"hold on to|holding on to|recall|ma(?:de|ke|king) a note|won'?t forget|will not forget|never forget|"
           r"updated? what I know|added? (?:it |that |this )?to (?:your|my|the) \w+)\b")
CLAIM = re.compile(rf"{_SPEAKER}(?:\W+\w+){{0,6}}?\W+(?P<mem>{_MEMORY})", re.I)
# "a note", "the record", "worth keeping a log": the memory word as a thing, not something done
THING = re.compile(r"\b(?:a|an|the|good|worth|nice|useful)\s+(?:\w+\s+)?$", re.I)
# "Your preference is noted", "That has been saved"
PASSIVE = re.compile(r"\b(?:is|are|has been|have been|was|were)\s+(?:now\s+|safely\s+|duly\s+)?"
                     r"(?:noted|saved|recorded|stored|remembered|logged|filed|committed to memory)\b", re.I)
# a sentence that is only the memory word: "Noted.", "Added to your notes, sir."
BARE = re.compile(r"^\W*(?:noted|saved|stored|logged|recorded|filed|jotted(?: down)?|remembered|committed|added)\b",
                  re.I)
NEGATION = re.compile(r"\b(?:not|no|never(?! forget)|nothing|couldn'?t|can'?t|cannot|don'?t|didn'?t|haven'?t|"
                      r"hasn'?t|wasn'?t|isn'?t|won'?t(?! forget)|unable|without)\b", re.I)
# offers and instructions: "Shall I save it?", 'Say "remember" and I will', "if you'd like me to note it"
OFFER = re.compile(r"\?\s*$|\b(?:shall I|should I|would you like|do you want|want me to|if you(?:'d)? (?:like|want)|"
                   r"say \W?remember|just say|ask me to)\b", re.I)
# recalling: "I remember you said...", "as I recall, your..." (present tense, about what the user said)
RECALL = re.compile(r"\b(?:I|let me) (?:remember|recall)\b(?! (?:that|this|it)\b\W*$)|\bas I recall\b"
                    r"|\byou (?:said|told me|mentioned)\b", re.I)
# "noted in your calendar as 4 pm": somewhere other than JARVIS's notes says it
ELSEWHERE = re.compile(r"\b(?:in|on) (?:your|the) (?:calendar|diary|schedule|ticket|email|page you)\b", re.I)


def claims_memory(text: str) -> bool:
    """The reply says it saved, noted or will remember something."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = sentence.strip()
        if not s or OFFER.search(s) or RECALL.search(s) or ELSEWHERE.search(s):
            continue
        if BARE.search(s):
            return True
        if PASSIVE.search(s) and not NEGATION.search(s):
            return True
        m = CLAIM.search(s)
        if m and not NEGATION.search(m.group(0)) and not THING.search(s[:m.start("mem")]):
            return True
    return False


# ---------- any action: a timer set, a message sent, a device changed ----------
# "Noted, sir.", "I've set a timer", "That's done", "Let me send that": a reply saying something was (or is
# about to be) done. A 2B model often says it and calls nothing.
ACTION_CLAIM = re.compile(
    r"^\W*(noted|saved|stored|added|sent|done|remembered|logged|recorded|deleted|removed|scheduled|set)\b"
    r"|\bI(?:'ve| have|'ll| will)?\s+(?:just\s+|now\s+|also\s+|already\s+)?(?:saved|noted|stored|remembered|added|"
    r"logged|recorded|written|jotted|made a note|sent|deleted|removed|updated|turned|switched|scheduled|set|started|"
    r"created|texted|messaged|called|notified|pinged|paused|resumed|skipped|locked|opened|launched|muted|moved)\b"
    r"|\b(?:I'll|I will) (?:remember|keep (?:that|this|it) in mind)"
    # "I'll set a notification...", "Let me save that.": a promise that a 2B model then doesn't keep
    r"|\b(?:I'll|I will|I'm going to|I am going to|I'm now|I am now|let me|let's)\s+(?:now\s+|just\s+|go ahead and\s+)?"
    r"(?:set|send|ping|notify|remind|open|play|pause|skip|text|message|call|turn|add|schedule|lock|save|note|start|"
    r"cancel|stop|mute|record|log|store|jot|move|create|put)\b"
    r"|\bproceeding to\b|\bthat'?s (?:done|sorted|taken care of|all set)\b|\b(?:it'?s|you'?re) all set\b"
    r"|\b(?:timer|reminder|alarm|alert|notification|message|text)\b[^.?!]{0,40}?\b(?:is|has been|was|are|have been)\s+"
    r"(?:now\s+|all\s+)?(?:set|scheduled|sent|started|created|on)\b"
    r"|\b(?:skill|automation)\b[^.?!]{0,60}?\b(?:now\s+(?:prints|says|shows|tells|does|runs|includes|will)|"
    r"(?:has been|is now)\s+(?:updated|changed|created|made|deleted|removed))\b",
    re.I)
_SKILL = re.compile(r"\b(?:skills?|automations?)\b", re.I)
_TIMER = re.compile(r"\b(?:timers?|reminders?|alarms?|alerts?|countdown|remind you)\b", re.I)
_MESSAGE = re.compile(r"\b(?:sent|send|texted|text|messaged|whatsapp(?:ed)?|called|call|rang|ring|dial(?:l?ed)?|"
                      r"notif(?:ied|y|ication)|pinged|ping)\b", re.I)
_DEVICE = re.compile(r"\b(?:paused?|resumed?|skipped|skip|locked|lock|opened|open|launched|launch|muted|mute|"
                     r"turned|turn|switched|switch|dimmed|dim|played|play|stopped|volume|lights?|fan|music)\b", re.I)
# The kind of tool that backs a claim of that kind: "I've set a timer" isn't backed by a notification.
KINDS = {
    "notes": frozenset({"kb_remember", "kb_journal", "kb_append_to_page", "kb_correct", "kb_move_item"}),
    "timer": frozenset({"set_timer", "cancel_timer", "set_reminder", "cancel_reminder"}),
    "message": frozenset({"adb_sms_send", "adb_whatsapp_send", "adb_call", "notify", "adb_call_answer",
                          "adb_call_end"}),
    "skill": frozenset({"create_skill", "update_skill", "delete_skill", "enable_skill"}),
}


def kind_of(name: str, read: bool) -> str | None:
    """What kind of action a tool that ran does: notes, timer, message, device; None for a read."""
    for kind, names in KINDS.items():
        if name in names:
            return kind
    return None if read else "device"


def claimed_kind(text: str, done: set[str]) -> str | None:
    """The kind of action the reply says was done (or is about to be) that no tool of that kind did this turn:
    "notes", "timer", "message", "device", or "action" for a claim of no particular kind with nothing changed.
    None when every claim in it is backed. done: the kinds of the tools that ran (kind_of)."""
    if claims_memory(text) and "notes" not in done:
        return "notes"
    unbacked = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = sentence.strip()
        if not s or OFFER.search(s) or not ACTION_CLAIM.search(s):
            continue
        kind = "skill" if _SKILL.search(s) else "timer" if _TIMER.search(s) else "message" if _MESSAGE.search(s) else \
            "device" if _DEVICE.search(s) else "action"
        if kind == "action" and not done or kind != "action" and kind not in done:
            unbacked.append(kind)
    # "That's done, sir. I've set a timer...": the timer is what to make happen
    return next((k for k in unbacked if k != "action"), unbacked[0] if unbacked else None)
