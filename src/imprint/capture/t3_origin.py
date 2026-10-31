"""T3's own record of who wrote a prompt, for a transport that cannot attest it.

T3 submits every prompt through the SDK, so a transcript entry reads the same
whether the operator typed it or an agent sent it. T3 does record the author of
each message. That record is read here, for one thread and one exact text, and
anything short of a clear match leaves the entry unverified.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .detector import strip_embedded_host_content

T3_ENTRYPOINT = "sdk-ts"
T3_THREAD_ENV = "T3CODE_THREAD_ID"
T3_HOME_ENV = "T3CODE_HOME"
_HUMAN_AUTHOR = "user"
# Sources tied to a person at a T3 client. "server" also covers older records
# whose client is unknown, so it does not count.
_CLIENT_SOURCES = frozenset({"web", "mobile"})
_RECENT_PROMPTS = 64
_BUSY_TIMEOUT_SECONDS = 0.25
_PROMPTS_QUERY = (
    "SELECT payload_json FROM orchestration_v2_projection_messages "
    "WHERE thread_id = ? AND role = 'user' ORDER BY created_at DESC LIMIT ?"
)


def _comparable(text: str) -> str:
    return strip_embedded_host_content(text).strip()


def _database_path(environ: Mapping[str, str]) -> Path:
    home = environ.get(T3_HOME_ENV, "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".t3"
    return base / "userdata" / "statev2.sqlite"


def _read_prompts(database: Path, thread_id: str) -> tuple[Mapping[str, Any], ...]:
    """Return the thread's newest prompts. Any doubt reads as no record at all."""
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS,
        )
    except (sqlite3.Error, OSError, ValueError):
        return ()
    try:
        rows = connection.execute(_PROMPTS_QUERY, (thread_id, _RECENT_PROMPTS)).fetchall()
        prompts = tuple(json.loads(payload) for (payload,) in rows)
    except (sqlite3.Error, ValueError, TypeError):
        return ()
    finally:
        connection.close()
    return prompts if all(isinstance(prompt, dict) for prompt in prompts) else ()


class T3HostOrigin:
    """Answers whether T3 records this exact prompt as written by a person."""

    def __init__(self, thread_id: str, database: Path) -> None:
        self._thread_id = thread_id
        self._database = database
        self._prompts: tuple[Mapping[str, Any], ...] | None = None

    def _record_for(self, text: str) -> Mapping[str, Any] | None:
        """Return the newest prompt with these words, whoever wrote it.

        Newest wins so an agent repeating the operator's words is judged as the
        agent's message, not credited to the older human one.
        """
        wanted = _comparable(text)
        if not wanted:
            return None
        if self._prompts is None:
            self._prompts = _read_prompts(self._database, self._thread_id)
        for prompt in self._prompts:
            body = prompt.get("text")
            if isinstance(body, str) and _comparable(body) == wanted:
                return prompt
        return None

    def _human_record(self, text: str) -> Mapping[str, Any] | None:
        prompt = self._record_for(text)
        if (
            prompt is None
            or prompt.get("createdBy") != _HUMAN_AUTHOR
            or prompt.get("creationSource") not in _CLIENT_SOURCES
            or prompt.get("senderThreadId")
            or prompt.get("notification")
        ):
            return None
        return prompt

    def attests(self, entry: Mapping[str, Any], text: str) -> bool:
        return entry.get("entrypoint") == T3_ENTRYPOINT and self._human_record(text) is not None

    def evidence(self, text: str) -> dict[str, str] | None:
        """Return the content-free basis for an attested prompt."""
        prompt = self._human_record(text)
        if prompt is None:
            return None
        return {
            "host": "t3",
            "basis": "t3_message_record",
            "created_by": str(prompt["createdBy"]),
            "creation_source": str(prompt["creationSource"]),
        }


def t3_host_origin(
    config: Mapping[str, Any], environ: Mapping[str, str] | None = None,
) -> T3HostOrigin | None:
    """Build the adapter only when it is enabled and this run belongs to a T3 thread."""
    if config.get("t3_capture_origin") is not True:
        return None
    values = os.environ if environ is None else environ
    thread_id = values.get(T3_THREAD_ENV, "").strip()
    if not thread_id:
        return None
    return T3HostOrigin(thread_id, _database_path(values))
