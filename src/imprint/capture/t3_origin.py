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

from .detector import operator_words, strip_effort_prefix
from .provenance import MID_TURN_SOURCE
from .transcript import _well_formed

T3_ENTRYPOINT = "sdk-ts"
T3_THREAD_ENV = "T3CODE_THREAD_ID"
T3_HOME_ENV = "T3CODE_HOME"
T3_RECORD_UNAVAILABLE_REASON = "t3_record_unavailable"
# The records were read and could not judge the prompt. A nested run names another
# session and a prompt can reach Claude in a form T3 does not record, so these are
# counted. No provider row with a session id at all means T3 changed its shape.
T3_SESSION_REF_UNREADABLE_REASON = "t3_session_ref_unreadable"
T3_SESSION_UNMATCHED_REASON = "t3_session_unmatched"
T3_PROMPT_UNMATCHED_REASON = "t3_prompt_unmatched"
# T3's "No project" chats live in this folder of its home, as a project whose root
# it is. Test scripts drive the app there and T3 records what they type as
# user/web, so no prompt in one is credited, a person's included.
T3_SCRATCH_THREAD_REASON = "t3_scratch_thread"
_SCRATCH_FOLDER = "scratch"
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
_THREAD_QUERY = (
    "SELECT thread.payload_json, project.workspace_root FROM orchestration_v2_projection_threads AS thread "
    "LEFT JOIN projection_projects AS project ON project.project_id = thread.project_id "
    "WHERE thread.thread_id = ?"
)
_PROVIDER_THREADS_QUERY = (
    "SELECT payload_json FROM orchestration_v2_projection_provider_threads WHERE thread_id = ?"
)
# T3's Implement button sends the assistant's plan after this line, recorded as
# user/web like anything a person types. No field marks it.
_PLAN_PREFIX = "PLEASE IMPLEMENT THIS PLAN:\n"
# T3 stores a context chip as this link and sends the provider a marker for it.
_CONTEXT_HREF = "](t3-context://v1/"
_CONTEXT_LINK = re.compile(
    r"!?\[([^\]\n]{0,512})\]\(t3-context://v1/([a-z][a-z0-9-]{0,39})/([A-Za-z0-9_-]{1,128})\)",
)
_CHIP_LABEL_CHARS = 512
_ENVELOPE_TAG = re.compile(r"<(?=/?(?:t3_context|context)\b)", re.I)
# A context handoff or a restart note goes in front of the message, at send time.
_HANDOFF_MARKER = "\n\nUser message:\n"


def _provider_marker(link: re.Match[str]) -> str:
    """Rewrite one stored context chip as T3 does before sending it."""
    label, kind, context_id = link.groups()
    label = " ".join(re.sub(r"[\[\]\\\r\n]", " ", label).split())[:200] or kind
    label = _ENVELOPE_TAG.sub("&lt;", " ".join(label.replace(";", " ").split()))
    name = kind.replace("-", " ")
    return f"[{name[:1].upper()}{name[1:]}: {label}; ref={context_id}]"


def _projected_chips(text: str) -> str:
    """Rewrite every stored chip in ``text``, working out from each link's fixed middle.

    A label stops at the line, the last "]" or its length limit, so the first "["
    in that stretch opens it, and a long run of brackets costs nothing.
    """
    parts, done, href = [], 0, text.find(_CONTEXT_HREF)
    while href >= 0:
        floor = max(done, href - _CHIP_LABEL_CHARS - 1)
        floor = max(floor, text.rfind("]", floor, href) + 1, text.rfind("\n", floor, href) + 1)
        opener = text.find("[", floor, href)
        start = opener - 1 if opener > done and text[opener - 1] == "!" else opener
        link = _CONTEXT_LINK.match(text, start) if opener >= 0 else None
        if link is not None:
            parts += (text[done:start], _provider_marker(link))
            done = link.end()
        href = text.find(_CONTEXT_HREF, max(href + 1, done))
    parts.append(text[done:])
    return "".join(parts)


def _comparable(text: str) -> str:
    """Return a prompt in one form, read from T3's row or from the transcript.

    The effort prefix can be added at send time only, so a row and its entry can
    differ by it. A transcript line has its lone surrogates replaced as it is read,
    so a row needs the same.
    """
    return operator_words(_projected_chips(_well_formed(text)))


def _database_path(environ: Mapping[str, str]) -> Path | None:
    """Return the store's path, or None when the home named cannot be resolved."""
    home = environ.get(T3_HOME_ENV, "").strip()
    try:
        base = Path(home).expanduser() if home else Path.home() / ".t3"
    except RuntimeError:
        return None
    return base / "userdata" / "statev2.sqlite"


def t3_settings_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """Return T3's settings file, which sits beside its store, if its home resolves."""
    database = _database_path(os.environ if environ is None else environ)
    return None if database is None else database.with_name("settings.json")


def _connect(database: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS,
        )
    except (sqlite3.Error, OSError, ValueError, RuntimeError):
        return None


def _thread_records(connection: sqlite3.Connection, thread_id: str) -> tuple[Any, Any, tuple[Any, ...]]:
    """Return the thread's row, its decoded payload and its provider rows."""
    thread = connection.execute(_THREAD_QUERY, (thread_id,)).fetchone()
    payload = None if thread is None else json.loads(thread[0])
    rows = connection.execute(_PROVIDER_THREADS_QUERY, (thread_id,)).fetchall()
    return thread, payload, tuple(json.loads(row) for (row,) in rows)


def _thread_folders(thread: Any, payload: Any) -> tuple[Any, ...] | None:
    """Return the thread's folders, or None when the store holds no such thread.

    T3 keeps a test server's state apart, so a run can name a thread that this
    store does not hold.
    """
    if thread is None:
        return None
    return tuple(folder for folder in (payload.get("worktreePath"), thread[1]) if folder is not None)


def _read_records(
    database: Path, thread_id: str,
) -> tuple[tuple[Any, ...] | None, tuple[Any, ...], tuple[tuple[Any, Mapping[str, Any]], ...]] | None:
    """Return the thread's folders, provider rows and newest prompts, or None if unreadable."""
    connection = _connect(database)
    if connection is None:
        return None
    try:
        thread, payload, providers = _thread_records(connection, thread_id)
        rows = connection.execute(_PROMPTS_QUERY, (thread_id, _RECENT_PROMPTS)).fetchall()
        prompts = tuple((message_id, json.loads(payload)) for (message_id, payload) in rows)
    except (sqlite3.Error, ValueError, TypeError, RecursionError):
        return None
    finally:
        connection.close()
    # A row of any other shape could be a newer author, so the window is unusable.
    usable = (thread is None or isinstance(payload, dict)) and all(
        isinstance(prompt, dict) and isinstance(prompt.get("text"), str) for _, prompt in prompts
    )
    if not usable:
        return None
    return _thread_folders(thread, payload), providers, prompts


def _read_placement(database: Path, thread_id: str) -> tuple[tuple[Any, ...] | None, tuple[Any, ...]] | None:
    """Return the thread's folders and provider rows, or None if unreadable.

    This reads no prompt, so it costs a prompt hook as little as it can.
    """
    connection = _connect(database)
    if connection is None:
        return None
    try:
        thread, payload, providers = _thread_records(connection, thread_id)
    except (sqlite3.Error, ValueError, TypeError, RecursionError):
        return None
    finally:
        connection.close()
    if thread is not None and not isinstance(payload, dict):
        return None
    return _thread_folders(thread, payload), providers


def _scratch_placement(folders: tuple[Any, ...] | None, database: Path) -> bool | None:
    """Say whether T3's records place a thread in its scratch folder.

    True when a folder is in scratch, False when every folder can be read and none
    is, and None when the records cannot say: no thread row, no folder, or a folder
    that is not absolute or cannot be read.
    """
    if not folders:
        return None
    try:
        scratch = (database.parents[1] / _SCRATCH_FOLDER).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    placement: bool | None = False
    for folder in folders:
        try:
            path = Path(folder)
            if not path.is_absolute():
                placement = None
            elif path.resolve().is_relative_to(scratch):
                return True
        except (OSError, RuntimeError, TypeError, ValueError):
            placement = None
    return placement


def _in_scratch(folders: tuple[Any, ...] | None, database: Path) -> bool:
    """Answer whether to credit nothing because this thread may be in scratch.

    A folder that cannot be read is not credited, since it could be that one. So
    is a session with no thread row, which leaves no folder to read.
    """
    return _scratch_placement(folders, database) is not False


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

    def __init__(self, thread_id: str, session_id: str, database: Path | None) -> None:
        self._thread_id = thread_id
        self._session_id = session_id
        self._database = database
        self._index: dict[str, tuple[Any, Mapping[str, Any]]] | None = None
        self._session_problem: str | None = None
        self._miss: str | None = None
        self.read_failed = False

    @property
    def skip_reason(self) -> str | None:
        """Why the records could not judge the prompt last asked about, if they could not.

        A refused record is a judgment, so it gives no reason here: that is what
        an agent's prompt looks like, and it stays an ordinary skip.
        """
        return T3_RECORD_UNAVAILABLE_REASON if self.read_failed else self._miss

    def _prompts_by_words(self) -> Mapping[str, tuple[Any, Mapping[str, Any]]]:
        """Return this session's prompts by their words, reading T3 once.

        Newest wins so an agent repeating the operator's words is judged as the
        agent's message, not credited to the older human one. A run inherits
        T3CODE_THREAD_ID from whatever started it, so unless a provider row names
        this exact session the thread's prompts are not this transcript's at all.
        A scratch thread's prompts are not read either, so none can be matched.
        """
        if self._index is None:
            self._index = {}
            records = None if self._database is None else _read_records(self._database, self._thread_id)
            if records is None:
                self.read_failed = True
                return self._index
            folders, providers, prompts = records
            sessions = [_native_session(row) for row in providers]
            if self._session_id in sessions:
                if _in_scratch(folders, self._database):
                    self._session_problem = T3_SCRATCH_THREAD_REASON
                else:
                    for message_id, prompt in prompts:
                        self._index.setdefault(_comparable(prompt["text"]), (message_id, prompt))
            elif providers and not any(isinstance(session, str) and session for session in sessions):
                self._session_problem = T3_SESSION_REF_UNREADABLE_REASON
            else:
                self._session_problem = T3_SESSION_UNMATCHED_REASON
        return self._index

    def _record_for(self, text: str) -> tuple[str, tuple[Any, Mapping[str, Any]]] | None:
        """Return the part of an entry T3 recorded, with the newest record of it.

        What precedes the handoff marker is T3's, so an entry with one is matched
        on what follows it, and only when the whole entry matches nothing: an agent
        that sent text with that marker in it is still judged as the agent.
        """
        index = self._prompts_by_words()
        wanted = _comparable(text)
        if wanted and wanted in index:
            return text, index[wanted]
        message = text.rpartition(_HANDOFF_MARKER)[2]
        wanted = _comparable(message) if message != text else ""
        return (message, index[wanted]) if wanted and wanted in index else None

    def _human_record(self, text: str) -> tuple[str, str, Mapping[str, Any]] | None:
        found = self._record_for(text)
        if found is None:
            self._miss = self._session_problem or T3_PROMPT_UNMATCHED_REASON
            return None
        words, (message_id, prompt) = found
        self._miss = None
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
            or strip_effort_prefix(prompt["text"]).startswith(_PLAN_PREFIX)
        ):
            return None
        return words, message_id, prompt

    def in_scratch(self) -> bool:
        """Answer whether T3's records place this session's thread in its scratch folder.

        Capture refuses whatever it cannot place. Selection is the other way round:
        this turns a service off, so a thread the records cannot place keeps it, and
        only a clear placement counts. A run inherits T3CODE_THREAD_ID from whatever
        started it, so unless a provider row names this exact session the thread's
        folder says nothing about this run.
        """
        records = None if self._database is None else _read_placement(self._database, self._thread_id)
        if records is None:
            return False
        folders, providers = records
        if self._session_id not in [_native_session(row) for row in providers]:
            return False
        return _scratch_placement(folders, self._database) is True

    def attests(self, entry: Mapping[str, Any], text: str) -> bool:
        """Answer for an SDK prompt or a mid-turn one, which records no entrypoint."""
        self._miss = None
        transport = entry.get("entrypoint") == T3_ENTRYPOINT or entry.get("promptSource") == MID_TURN_SOURCE
        return transport and self._human_record(text) is not None

    def recorded_words(self, text: str) -> str:
        """Return only the part of an attested entry that T3 recorded."""
        record = self._human_record(text)
        return text if record is None else record[0]

    def evidence(self, text: str) -> dict[str, str] | None:
        """Return the content-free basis for an attested prompt."""
        record = self._human_record(text)
        if record is None:
            return None
        _, message_id, prompt = record
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
