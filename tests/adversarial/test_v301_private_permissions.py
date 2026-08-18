from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from imprint.backup import create_backup
from imprint.compiler import compile_spools, write_envelope
from imprint.permissions import secure_directory, secure_file, secure_tree, unsafe_posix_permissions, unsafe_private_permissions
from imprint.store import ImprintStore


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_tree_closes_permissive_umask_state(tmp_path):
    root = tmp_path / "operator"
    nested = root / "spool" / "node"
    nested.mkdir(parents=True, mode=0o777)
    database = root / "imprint.db"
    database.write_text("sensitive", encoding="utf-8")
    os.chmod(root, 0o755)
    os.chmod(nested, 0o755)
    os.chmod(database, 0o644)

    assert unsafe_posix_permissions(root)
    secure_tree(root)

    assert _mode(root) == 0o700
    assert _mode(root / "spool") == 0o700
    assert _mode(nested) == 0o700
    assert _mode(database) == 0o600
    assert unsafe_posix_permissions(root) == ()
    assert unsafe_private_permissions(root) == ()


def test_secure_helpers_refuse_symlinks(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(target_dir, target_is_directory=True)
    with pytest.raises(OSError, match="symlinked"):
        secure_directory(directory_link)

    target_file = tmp_path / "target.txt"
    target_file.write_text("secret", encoding="utf-8")
    file_link = tmp_path / "file-link"
    file_link.symlink_to(target_file)
    with pytest.raises(OSError, match="regular file"):
        secure_file(file_link)


def test_secure_file_tolerates_vanished_path(tmp_path):
    # SQLite drops -wal/-shm sidecars when the last connection closes, racing
    # every exists-then-tighten caller (_secure_sqlite_state); the 2026-08-17
    # session-start hook failure was FileNotFoundError on imprint.db-shm.
    ghost = tmp_path / "imprint.db-shm"
    assert secure_file(ghost) == ghost
    assert not ghost.exists()


def test_secure_file_still_refuses_present_irregular_path(tmp_path):
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(OSError, match="regular file"):
        secure_file(directory)


def test_unsafe_posix_scan_skips_vanished_stat(tmp_path):
    from imprint.permissions import _lmode

    assert _lmode(tmp_path / "vanished-sidecar") is None
    survivor = tmp_path / "loose.txt"
    survivor.write_text("x", encoding="utf-8")
    os.chmod(survivor, 0o644)
    assert _lmode(survivor) == 0o644
    assert unsafe_posix_permissions(tmp_path) == ("loose.txt",)


def test_secure_directory_hardens_new_parent_chain(tmp_path):
    root = tmp_path / "private" / "runtime" / "receipts"
    prior = os.umask(0)
    try:
        secure_directory(root)
    finally:
        os.umask(prior)
    assert _mode(tmp_path / "private") == 0o700
    assert _mode(tmp_path / "private" / "runtime") == 0o700
    assert _mode(root) == 0o700


def test_capture_compile_and_backup_remain_private_under_permissive_umask(
    tmp_path, capture_envelope,
):
    root = tmp_path / "operator"
    prior = os.umask(0)
    try:
        write_envelope(root, capture_envelope)
        store = ImprintStore(root / "imprint.db")
        assert compile_spools(root, store, compiler_authorized=True)["captured"] == 1
        backup = create_backup(store, root)
    finally:
        os.umask(prior)

    assert _mode(root) == 0o700
    assert _mode(store.path) == 0o600
    assert _mode(next((root / "spool").glob("*/*.json"))) == 0o600
    assert _mode(next((root / "runtime" / "acknowledgements").glob("*/*.json"))) == 0o600
    assert _mode(Path(backup["path"])) == 0o600
    assert _mode(Path(backup["receipt_path"])) == 0o600
    assert unsafe_posix_permissions(root) == ()
