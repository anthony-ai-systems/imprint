"""T3's own record of who wrote a prompt, for a transport that cannot attest it.

T3 submits every prompt through the SDK, so a transcript entry reads the same
whether the operator typed it or an agent sent it. T3 does record the author of
each message. That record is read here, for one thread, one Claude session and
one exact text, and anything short of a clear match leaves the entry unverified.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .detector import strip_embedded_host_content, strip_leading_host_blocks

T3_ENTRYPOINT = "sdk-ts"
T3_THREAD_ENV = "T3CODE_THREAD_ID"
T3_HOME_ENV = "T3CODE_HOME"
T3_RECORD_UNAVAILABLE_REASON = "t3_record_unavailable"
_HUMAN_AUTHOR = "user"
# Sources tied to a person at a T3 client. "server" also covers older records
# whose client is unknown, so it does not count.
_CLIENT_SOURCES = frozenset({"web", "mobile"})
_RECENT_PROMPTS = 64
_BUSY_TIMEOUT_SECONDS = 0.25
_PROMPTS_QUERY = (
    "SELECT message_id, payload_json FROM orchestration_v2_projection_messages "
    "WHERE thread_id = ? AND role = 'user' ORDER BY created_at DESC LIMIT ?"
)
_PROVIDER_THREADS_QUERY = (
    "SELECT payload_json FROM orchestration_v2_projection_provider_threads WHERE thread_id = ?"
)
# T3 adds this for Ultrathink effort, in the composer or only at send time, so a
# row and its transcript entry can differ by it. It is not the operator's word.
_EFFORT_PREFIX = "Ultrathink:\n"
# T3's Implement button sends the assistant's plan after this line, recorded as
# user/web like anything a person types. No field marks it.
_PLAN_PREFIX = "PLEASE IMPLEMENT THIS PLAN:\n"
# T3 stores a context chip as this link and sends the provider a marker for it.
_CONTEXT_HREF = "](t3-context://v1/"
_CONTEXT_LINK = re.compile(
    r"!?\[([^\]\n]{0,512})\]\(t3-context://v1/([a-z][a-z0-9-]{0,39})/([A-Za-z0-9_-]{1,128})\)",
)
_ENVELOPE_TAG = re.compile(r"<(?=/?(?:t3_context|context)\b)", re.I)


def _without_effort_prefix(text: str) -> str:
    return text.lstrip().removeprefix(_EFFORT_PREFIX)


def operator_words(text: str) -> str:
    """Return a T3 prompt without what the host put around the operator's words."""
    return strip_embedded_host_content(_without_effort_prefix(strip_leading_host_blocks(text)))


def _provider_marker(link: re.Match[str]) -> str:
    """Rewrite one stored context chip as T3 does before sending it."""
    label, kind, context_id = link.groups()
    label = " ".join(re.sub(r"[\[\]\\\r\n]", " ", label).split())[:200] or kind
    label = _ENVELOPE_TAG.sub("&lt;", " ".join(label.replace(";", " ").split()))
    name = kind.replace("-", " ")
    return f"[{name[:1].upper()}{name[1:]}: {label}; ref={context_id}]"


def _comparable(text: str) -> str:
    """Return a prompt in one form, read from T3's row or from the transcript."""
    return operator_words(_CONTEXT_LINK.sub(_provider_marker, text) if _CONTEXT_HREF in text else text)


def _database_path(environ: Mapping[str, str]) -> Path:
    home = environ.get(T3_HOME_ENV, "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".t3"
    return base / "userdata" / "statev2.sqlite"


def _read_records(
    database: Path, thread_id: str,
) -> tuple[tuple[Any, ...], tuple[tuple[Any, Mapping[str, Any]], ...]] | None:
    """Return the thread's provider rows and newest prompts, or None if unreadable."""
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS,
        )
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        rows = connection.execute(_PROVIDER_THREADS_QUERY, (thread_id,)).fetchall()
        providers = tuple(json.loads(payload) for (payload,) in rows)
        rows = connection.execute(_PROMPTS_QUERY, (thread_id, _RECENT_PROMPTS)).fetchall()
        prompts = tuple((message_id, json.loads(payload)) for (message_id, payload) in rows)
    except (sqlite3.Error, ValueError, TypeError, RecursionError):
        return None
    finally:
        connection.close()
    # A row of any other shape could be a newer author, so the window is unusable.
    return (providers, prompts) if all(isinstance(prompt, dict) for _, prompt in prompts) else None


def _native_session(provider_row: Any) -> Any:
    reference = provider_row.get("nativeThreadRef") if isinstance(provider_row, dict) else None
    return reference.get("nativeId") if isinstance(reference, dict) else None


def _names_a_guest(prompt: Mapping[str, Any]) -> bool:
    context = prompt.get("context")
    if context is None:
        return False
    return not isinstance(context, Mapping) or bool(context.get("guest"))


class T3HostOrigin:
    """Answers whether T3 records this exact prompt as written by a person."""

    def __init__(self, thread_id: str, session_id: str, database: Path) -> None:
        self._thread_id = thread_id
        self._session_id = session_id
        self._database = database
        self._index: dict[str, tuple[Any, Mapping[str, Any]]] | None = None
        self.read_failed = False

    def _prompts_by_words(self) -> Mapping[str, tuple[Any, Mapping[str, Any]]]:
        """Return this session's prompts by their words, reading T3 once.

        Newest wins so an agent repeating the operator's words is judged as the
        agent's message, not credited to the older human one. A run inherits
        T3CODE_THREAD_ID from whatever started it, so unless a provider row names
        this exact session the thread's prompts are not this transcript's at all.
        """
        if self._index is None:
            self._index = {}
            records = _read_records(self._database, self._thread_id)
            if records is None:
                self.read_failed = True
            elif any(_native_session(row) == self._session_id for row in records[0]):
                for message_id, prompt in records[1]:
                    body = prompt.get("text")
                    if isinstance(body, str):
                        self._index.setdefault(_comparable(body), (message_id, prompt))
        return self._index

    def _human_record(self, text: str) -> tuple[str, Mapping[str, Any]] | None:
        wanted = _comparable(text)
        record = self._prompts_by_words().get(wanted) if wanted else None
        if record is None:
            return None
        message_id, prompt = record
        source = prompt.get("creationSource")
        if (
            not isinstance(message_id, str)
            or not message_id
            or prompt.get("createdBy") != _HUMAN_AUTHOR
            or not isinstance(source, str)
            or source not in _CLIENT_SOURCES
            or prompt.get("senderThreadId")
            or prompt.get("notification")
            or prompt.get("scheduledTaskId")
            or _names_a_guest(prompt)
            or _without_effort_prefix(prompt["text"]).startswith(_PLAN_PREFIX)
        ):
            return None
        return message_id, prompt

    def attests(self, entry: Mapping[str, Any], text: str) -> bool:
        return entry.get("entrypoint") == T3_ENTRYPOINT and self._human_record(text) is not None

    def evidence(self, text: str) -> dict[str, str] | None:
        """Return the content-free basis for an attested prompt."""
        record = self._human_record(text)
        if record is None:
            return None
        message_id, prompt = record
        return {
            "host": "t3",
            "basis": "t3_message_record",
            "message_id": message_id,
            "created_by": str(prompt["createdBy"]),
            "creation_source": str(prompt["creationSource"]),
        }


def t3_host_origin(
    config: Mapping[str, Any], session_id: str | None, environ: Mapping[str, str] | None = None,
) -> T3HostOrigin | None:
    """Build the adapter only when it is enabled and this run belongs to a T3 thread."""
    if config.get("t3_capture_origin") is not True or not session_id:
        return None
    values = os.environ if environ is None else environ
    thread_id = values.get(T3_THREAD_ENV, "").strip()
    if not thread_id:
        return None
    return T3HostOrigin(thread_id, session_id, _database_path(values))
