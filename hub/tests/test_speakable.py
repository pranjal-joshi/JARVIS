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


def test_machine_strings_are_not_read_out():
    said = to_speech("Docs are at https://example.com/a/b?x=1 and id 3f2504e0-4f89-11d3-9a0c-0305e82c3301. "
                     "Phone serial 61271XEBF1YAF1, ref 132211559994, file C:/Users/me/notes.txt, app com.whatsapp.w4b.")
    for junk in ("https", "3f2504e0", "61271XEBF1YAF1", "132211559994", "notes.txt", "com.whatsapp"):
        assert junk not in said
    assert "the link" in said and "the path" in said


def test_ordinary_numbers_are_kept():
    said = to_speech("Rs 2,500.00 credited at 21:13 on 01-Nov-26; OTP 123456, call 1800 266 2, room 42.")
    for keep in ("2,500.00", "21:13", "01-Nov-26", "123456", "42"):
        assert keep in said
