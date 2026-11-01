"""Utility tools: calculator safety, private-address guard, memory forgetting."""

import asyncio

import pytest

from jarvis_hub.kb.graph import LogseqGraph
from jarvis_hub.tools.utility import _public_host, safe_eval


def test_calculator_does_maths_not_code():
    assert safe_eval("2^10 + sqrt(16)") == 1028
    assert round(safe_eval("15% of 80".replace("% of", "/100*")), 6) == 12
    for bad in ("__import__('os')", "open('x')", "(1).__class__", "9**99999"):
        with pytest.raises(ValueError):
            safe_eval(bad)


def test_private_addresses_are_refused():
    assert not _public_host("127.0.0.1")
    assert not _public_host("192.168.0.5")
    assert not _public_host("localhost")


def test_forget_only_touches_jarvis_pages(tmp_path):
    g = LogseqGraph(str(tmp_path))
    g.write_jarvis_page("jarvis/prefs", "likes tea")
    g.append_to_user_page("Groceries", "milk")
    with pytest.raises(ValueError):
        g.trash_jarvis_page("Groceries")
    assert g.trash_jarvis_page("jarvis/prefs").lower() == "jarvis/prefs"
    assert g.page("jarvis/prefs") is None and g.page("Groceries") is not None
    assert list((tmp_path / ".trash").iterdir())


def test_delete_items_and_clear_memory(tmp_path):
    g = LogseqGraph(str(tmp_path))
    g.append_to_user_page("Groceries", "- milk\n  - semi-skimmed\n- eggs\n- TODO buy bread")
    g.write_jarvis_page("jarvis/a", "x")
    g.write_jarvis_page("jarvis/b", "y")
    assert g.delete_blocks("Groceries", "MILK") == 1
    assert [b.text for b in g.page("Groceries").blocks] == ["eggs", "TODO buy bread"]
    with pytest.raises(ValueError):
        g.delete_blocks("Groceries", "e")  # too vague
    assert sorted(n.lower() for n in g.trash_all_memory()) == ["jarvis/a", "jarvis/b"]
    assert g.page("Groceries") is not None
    assert g.trash_page("Groceries") == "Groceries" and g.page("Groceries") is None
