from jarvis_hub.voice.speakable import to_speech


def test_plain_text_untouched():
    assert to_speech("The kitchen light is on, sir.") == "The kitchen light is on, sir."


def test_markdown_is_stripped():
    out = to_speech("## Result\n- **Two** lights are on\n- see [the log](http://x)")
    assert out == "Result. Two lights are on. see the log."


def test_code_and_tables_are_not_spoken():
    out = to_speech("Run this:\n```bash\nrm -rf /tmp/x\n```\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert out == "Run this."
    assert to_speech("```py\nprint(1)\n```") == ""


def test_runaway_answers_are_cut_quietly_at_a_sentence():
    text = " ".join(f"Sentence number {i} is here." for i in range(60))
    out = to_speech(text + "\n\n— claude · opus (9 s)")
    assert len(out) <= 500 and out.endswith("is here.") and "claude" not in out and "screen" not in out
    assert out.startswith("Sentence number 0 is here.")
