"""Style enforced in code: lists, bold, offers and length, the same streamed or whole."""

from jarvis_hub.agent.style import SentenceGate, tidy
from jarvis_hub.agent.tool_agent import _fit
from jarvis_hub.config import Source

LISTY = ("Here are the highlights:\n\n- **PLEXOS 12** released in March.\n- PLEXOS 11 R05 in August.\n"
         "- Product updates for August.\n\nWould you like more detail, sir?")


def test_lists_bold_offers_and_length():
    assert tidy(LISTY, Source.WEB) == ("Here are the highlights. PLEXOS 12 released in March. PLEXOS 11 R05 in August. "
                                      "Product updates for August.")
    assert tidy(LISTY, Source.SATELLITE) == "Here are the highlights. PLEXOS 12 released in March."
    assert tidy("Would you like me to set a timer?", Source.WEB) == "Would you like me to set a timer?"  # only an offer
    assert tidy("It's 24 degrees in Pune 😊. Let me know if you need anything else.", Source.PHONE) == \
        "It's 24 degrees in Pune."
    assert tidy("Canberra, sir.\n— JARVIS", Source.WEB) == "Canberra, sir."
    code = "Run this:\n```\nls -la\n```\nLet me know if it works."
    assert tidy(code, Source.WEB) == code  # code is left alone


def test_streamed_matches_whole():
    for source in Source:
        out = []
        gate = SentenceGate(out.append, source)
        for i in range(0, len(LISTY), 7):  # arrives in odd-sized pieces
            gate.feed(LISTY[i:i + 7])
        gate.flush()
        assert "".join(out) == tidy(LISTY, source), source


def test_fit_keeps_the_system_prompt():
    convo = [{"role": "system", "content": "RULES"}]
    for i in range(30):
        convo += [{"role": "user", "content": f"question {i} " + "x" * 400},
                  {"role": "assistant", "content": "answer " + "y" * 400}]
    convo += [{"role": "user", "content": "now"}, {"role": "tool", "tool_call_id": "a", "content": "z" * 9000}]
    _fit(convo, [], 2000)
    assert convo[0]["content"] == "RULES" and convo[-2]["content"] == "now"
    assert len(convo[-1]["content"]) < 2000 and len(convo) < 10
