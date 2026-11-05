"""Spoken symbols become characters before the model sees them (voice/spoken.py)."""

from jarvis_hub.voice.spoken import symbols


def test_spoken_symbols_between_words_become_characters():
    assert symbols("add it to work slash AI Studio page") == "add it to work/AI Studio page"
    assert symbols("open https colon slash slash dreadedlama dot com slash articles") == \
        "open https://dreadedlama.com/articles"
    assert symbols("file is C colon back slash users") == "file is C:\\users"
    assert symbols("my email is pranjal at the rate example dot com") == "my email is pranjal@example.com"
    assert symbols("tag it hashtag work") == "tag it #work"
    assert symbols("e dash mail") == "e-mail"


def test_a_word_on_its_own_stays_a_word():
    assert symbols("play Slash") == "play Slash"
    assert symbols("a dash of salt") == "a dash of salt"
    assert symbols("what is the time") == "what is the time"
