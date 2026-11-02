"""SQLite removes its side files when the last connection closes, at any moment."""

from __future__ import annotations

from pathlib import Path

import pytest

from imprint.permissions import secure_file
from imprint.store import ImprintStore


def test_a_file_that_goes_while_it_is_checked_has_no_modes_to_leak(tmp_path, monkeypatch):
    gone = tmp_path / "imprint.db-shm"
    monkeypatch.setattr(Path, "exists", lambda self, **_: True)
    assert secure_file(gone) == gone


def test_a_file_that_is_there_and_is_not_regular_still_fails_closed(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    link = tmp_path / "link"
    link.symlink_to(folder)
    for target in (folder, link):
        with pytest.raises(OSError, match="not a regular file"):
            secure_file(target)


def test_a_wal_that_goes_while_it_is_checked_does_not_stop_the_drain(tmp_path, monkeypatch):
    store = ImprintStore(tmp_path / "imprint.db")
    store.initialize()
    real = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self, **kw: str(self).endswith("-wal") or real(self, **kw))
    assert store._drain_stale_sidecars(store.path.resolve()) == "clean"
