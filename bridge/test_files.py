"""File tools: read-only, inside the shared folders only, no secrets."""

import pytest

import device_bridge as b


@pytest.fixture
def shared(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    (notes / "trips").mkdir(parents=True)
    (notes / "packing.md").write_text("- passport\n- charger", encoding="utf-8")
    (notes / ".env").write_text("TOKEN=x", encoding="utf-8")
    (notes / "passwords.txt").write_text("hunter2", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    monkeypatch.setattr(b, "FILE_ROOTS", {"notes": notes.resolve()})
    return notes


def test_reads_inside_the_shared_folder(shared):
    assert b.fs_read({"path": "notes/packing.md"}).startswith("- passport")
    listing = b.fs_list({"path": "notes"})
    assert "packing.md" in listing and "trips/" in listing
    assert ".env" not in listing and "passwords" not in listing
    assert b.fs_list({}) == "Shared folders: notes"


@pytest.mark.parametrize("path", ["notes/../outside.txt", "notes/passwords.txt", "notes/.env", "docs/x.txt",
                                  "C:/Windows/win.ini"])
def test_refuses_everything_else(shared, path):
    with pytest.raises(b.ActionError):
        b.fs_read({"path": path})
