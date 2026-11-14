import os

import pytest

from jarvis_hub import files
from jarvis_hub.files import write_atomic


def test_write_atomic_waits_out_a_file_held_open_briefly(monkeypatch, tmp_path):
    real, calls = os.replace, []

    def busy_twice(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(files.os, "replace", busy_twice)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    write_atomic(tmp_path / "people.md", "hi")
    assert (tmp_path / "people.md").read_text() == "hi" and len(calls) == 3


def test_write_atomic_gives_up_without_leaving_a_temp_file(monkeypatch, tmp_path):
    monkeypatch.setattr(files.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(5, "denied")))
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        write_atomic(tmp_path / "people.md", "hi")
    assert list(tmp_path.iterdir()) == []
