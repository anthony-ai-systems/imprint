from __future__ import annotations

import socket
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import NamedTuple

import pytest

from imprint.capture.schema import build_capture_envelope, new_urn


def _live_hook_failure_spool() -> Path | None:
    """The real operator's hook-failure spool, resolved from the environment
    as it stood before any fixture overrode it -- hence import time."""
    try:
        from imprint.config import load_config, resolved_operator_root

        return resolved_operator_root(load_config()) / "logs" / "hook-failures"
    except Exception:
        return None


_LIVE_SPOOL = _live_hook_failure_spool()


class SpoolBaseline(NamedTuple):
    path: Path | None
    names: frozenset[str]


def spool_names(path: Path | None) -> frozenset[str]:
    if path is None or not path.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in path.iterdir())


@pytest.fixture(scope="session", autouse=True)
def operator_spool_guard():
    """Fail the run if any test adds records to the live operator spool.

    Only additions fail the guard: the live install may legitimately prune
    aged records while the suite runs. A failure here means some test
    resolved the operator's real config or data root -- the isolation in
    ``isolated_data_root`` below is the fix, not a carve-out here.
    """
    baseline = SpoolBaseline(_LIVE_SPOOL, spool_names(_LIVE_SPOOL))
    yield baseline
    leaked = sorted(spool_names(baseline.path) - baseline.names)
    assert not leaked, (
        f"test suite leaked records into the live operator spool {baseline.path}: {leaked}"
    )


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test may resolve the operator's real config or data root.

    ``config_path()`` honors IMPRINT_CONFIG and ``default_data_root()`` honors
    IMPRINT_DATA_ROOT, and spawned CLI/hook children inherit ``os.environ``,
    so this pins both in-process and subprocess resolution into ``tmp_path``.
    The IMPRINT_CONFIG target intentionally does not exist: ``load_config``
    then falls back to packaged defaults (operator ``default``) rooted at the
    isolated data root. Tests that need a specific config still win by
    setting IMPRINT_CONFIG themselves. T3's records are pinned the same way, so
    a run inside a T3 thread cannot reach the real ones.
    """
    data_root = tmp_path / "imprint-isolated-data"
    monkeypatch.setenv("IMPRINT_CONFIG", str(tmp_path / "imprint-isolated-config.json"))
    monkeypatch.setenv("IMPRINT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("T3CODE_HOME", str(tmp_path / "t3-isolated"))
    monkeypatch.delenv("T3CODE_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return data_root


class NetworkBlocked(OSError):
    """Raised by every outbound network primitive while the suite runs."""


_NETWORK_FAMILIES = {socket.AF_INET, getattr(socket, "AF_INET6", socket.AF_INET)}
# Socket methods that can put a packet on the wire: connect for streams, and
# sendto/sendmsg for connectionless datagrams that never call connect().
_SOCKET_SENDS = tuple(name for name in ("connect", "connect_ex", "sendto", "sendmsg") if hasattr(socket.socket, name))
# Every module-level resolver or connector that can reach a name server.
_RESOLVERS = (
    "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo",
)


def _blocked(*args, **kwargs):
    raise NetworkBlocked("outbound network is disabled for the test suite")


def _guarded(real):
    def guarded(self, *args, **kwargs):
        if self.family in _NETWORK_FAMILIES:
            _blocked()
        return real(self, *args, **kwargs)

    return guarded


_GUARDED_SENDS = {name: _guarded(getattr(socket.socket, name)) for name in _SOCKET_SENDS}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make outbound network impossible in-process for every test.

    Inet sockets cannot connect or send datagrams, every name-resolution
    function and ``create_connection`` fail, and ``urllib.request.urlopen``
    raises. Subprocesses spawned by tests inherit ``IMPRINT_SELECTOR=off``,
    which forces deterministic retrieval, and no selector API key from the
    developer's environment. Tests that exercise the selector in-process
    remove the override explicitly.
    """
    for name, guarded in _GUARDED_SENDS.items():
        monkeypatch.setattr(socket.socket, name, guarded)
    for name in _RESOLVERS:
        monkeypatch.setattr(socket, name, _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.setenv("IMPRINT_SELECTOR", "off")
    for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def capture_envelope():
    text = "Do not hide a failed source; say which source failed because missing evidence changes the conclusion."
    envelope = build_capture_envelope(
        operator_id=new_urn("operator"),
        session_id=new_urn("session"),
        node_id="workstation-a",
        case_description="Reviewing a multi-source research synthesis",
        raw_operator_text=text,
        call_type="correct",
        capture_mechanism="explicit_cli",
        captured_by="imprint-test",
        reason="Missing evidence changes the conclusion.",
        captured_at="2026-07-14T18:00:00Z",
    )
    return deepcopy(envelope)
