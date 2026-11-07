"""Client-work reader and carrier queue, for Codex prompts only (v0).

Fixed code: no model and no network. Nothing happens unless a queue file is named
in config. For a prompt a person typed, the reader takes up to three
client-naming signals, each from a client list read at run time:

- ``folder``: the thread's starting folder is inside one of a client's folders;
- ``thread_name``: the thread's name starts with one of a client's prefixes;
- ``named``: the prompt names the client, a client person or a client domain,
  through one of the list's strong patterns.

Two signals of different kinds that name the same client mark client work for that
client. One signal marks the client unknown, so a card can ask later. Signals that
name different clients mark it unknown too, and never pick one. No signal means it
is not client work, and nothing is written. A weak pattern is recorded with the
row but never counts as a signal.

A client person's first name is a backing signal (``first_name``). It counts as a
second, agreeing signal only when another signal already names that same client;
alone, or with only a weak pattern, it makes nothing. A first name that people of
two rooms share counts for neither, and so does the first name of a person who
spans rooms. A first name pointing at another client than the other signals makes
the client unknown. A first name inside a name or address a pattern already matched
is not counted again.

Each row says what the card must ask: ``client`` when the client is unknown, and
``speaker`` when the prompt has no feedback-shape flag, so whether it is a paste, a
paraphrase or the person's own words is unknown.

Paste shape (a colon lead, a lone ``====`` run, timestamp lines, Slack
name-and-time lines, image attachments) marks the prompt as carrying feedback. It
is never a client signal.

Rows come only from threads a person started, as Codex's own records say, and only
from words a person typed. A subagent, an automation, a scripted run, or a prompt
that is harness or automation text makes none.

The carrier row is a pointer, not a copy. It holds ids, a hash of the text, the
time, flags, the client guess with its signals, Loom ids, image paths, a speaker
guess, the channel, the carrier and the round, and no word of the message.

The carrier step never blocks or fails a prompt. On any error it records nothing
and prints nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from imprint.config import CLIENT_CARRIER_CLIENTS as CLIENTS_KEY
from imprint.config import CLIENT_CARRIER_QUEUE as QUEUE_KEY

ROW_SCHEMA = "imprint.carrier/0"
CHANNEL_CODEX = "codex_prompt"

CODEX_HOME_ENV = "CODEX_HOME"
CODEX_STATE_FILE = "state_5.sqlite"
# Codex's own database is opened read-only, and a lock is waited on this long at most.
_BUSY_TIMEOUT_SECONDS = 0.25

# A prompt is scanned up to this many characters; the hash covers all of it.
SCAN_CHARS = 256 * 1024
# An event larger than this is not read at all.
MAX_EVENT_BYTES = 8 * 1024 * 1024

SIGNAL_KINDS = ("folder", "thread_name", "named", "first_name")
CLIENT = "client"
UNKNOWN = "unknown"
NONE = "none"
# A signal of this strength only backs up another signal for the same client.
BACKING = "backing"
ASK_CLIENT = "client"
ASK_SPEAKER = "speaker"

# Rooms of these kinds are clients. Anything else in the list (own ventures,
# for one) names no client here.
_CLIENT_ROOM_KINDS = frozenset({"client", "client_rules_core_agents"})
# Prefix patterns belong to thread names, which are matched by prefix only.
_TEXT_SIGNALS = frozenset({"room_name", "domain", "address", "person_full_name", "product"})
_FOLDER_FIELDS = ("checkouts", "worktrees", "duplicates", "extra_folders")

# Codex records a thread a person started with this thread source, from one of
# these interactive surfaces. ``exec`` is a scripted run and is never a person's.
_PERSON_THREAD_SOURCE = "user"
_PERSON_SOURCES = frozenset({"vscode", "cli"})
# Hook payload fields Codex sets on a subagent's prompt.
_SUBAGENT_FIELDS = ("agent_id", "agent_type", "parent_session_id")

# Text Codex or an automation puts in a user-role message. A prompt that starts
# with one of these was not typed by a person.
MACHINE_PREFIXES = (
    "<environment_context",
    "<user_instructions",
    "<heartbeat",
    "<external_codex_apps_open_page",
    "<subagent_notification",
    "<turn_aborted",
    "# AGENTS.md instructions for",
)

_FILES_HEADER = re.compile(r"\A\s*# Files (?:mentioned|pasted) by the user\b")
_REQUEST_LINE = re.compile(r"^## My request(?: for Codex)?:[ \t]*$", re.M)
_HEADER_FILE = re.compile(r"^## [^\n]*?: (\S[^\n]*?)[ \t]*$", re.M)
_IMAGE_TAG = re.compile(r'<image name=\[[^\]\n]{0,200}\] path="([^"\n]{1,4096})"')
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff")
_LOOM = re.compile(r"(?i)\bloom\.com/(?:share|embed|v)/([0-9a-f]{32})\b")
_RULE_RUN = re.compile(r"^[ \t]*={4,}[ \t]*$")
_TIMESTAMP_LINE = re.compile(r"^[ \t]*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?[\])]?(?=[ \t]|$|[-–—:])")
# Slack separates a name from its time with ordinary or non-breaking spaces.
_GAP = "[ \\t\u00a0]"
_NAME = rf"[A-Z][\w.'\u2019-]{{0,40}}(?:{_GAP}[A-Z][\w.'\u2019-]{{0,40}}){{0,3}}"
_CLOCK = rf"\d{{1,2}}:\d{{2}}{_GAP}?[AaPp][Mm]"
_SLACK_LINES = (
    # A line that is only a name and a time, as Slack copies a message's header.
    re.compile(rf"^{_GAP}*({_NAME}){_GAP}{{1,4}}\[?{_CLOCK}\]?{_GAP}*$"),
    # A name and a bracketed time closing a line, as Slack copies a quoted message.
    re.compile(rf"(?:^|{_GAP})({_NAME}){_GAP}{{1,4}}\[{_CLOCK}\]{_GAP}*$"),
    # A bracketed time, then a name and a colon, as older exports write it.
    re.compile(rf"^{_GAP}*\[{_CLOCK}\]{_GAP}+({_NAME}):{_GAP}"),
)
_LEAD_CHARS = 300
TIMESTAMP_RUN = 3

SHAPE_FLAGS = ("colon_lead", "rule_run", "timestamp_lines", "slack_name_time", "image_attachment")
_TEXT_SHAPES = frozenset({"colon_lead", "rule_run", "timestamp_lines", "slack_name_time"})

# Cloud-synced folders, where a private queue must not live.
_SYNC_MARKERS = ("Dropbox", "OneDrive", "CloudStorage", "Google Drive", "Mobile Documents", "iCloud")
# Home folders that iCloud can sync whole, with no marker in the path.
_SYNCED_HOME_FOLDERS = ("Documents", "Desktop")


def _synced_home_folders() -> tuple[str, ...]:
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        return ()
    return tuple(str(home / name) for name in _SYNCED_HOME_FOLDERS)


def _inside_a_repository(resolved: str) -> bool:
    """True when any folder above the file holds ``.git``, as a folder or a worktree's file."""
    try:
        return any(os.path.lexists(os.path.join(parent, ".git")) for parent in Path(resolved).parents)
    except (OSError, ValueError):
        return True


# ---------------------------------------------------------------- client list


@dataclass(frozen=True)
class Person:
    name: str
    first: str


@dataclass(frozen=True)
class Room:
    room_id: str
    prefixes: tuple[str, ...]
    folders: tuple[str, ...]
    strong: tuple[tuple[re.Pattern[str], str], ...]
    weak: tuple[tuple[re.Pattern[str], str], ...]
    people: tuple[Person, ...]
    # First names that belong to this room's people and to no other room.
    first_names: tuple[str, ...] = ()
    first_name_re: re.Pattern[str] | None = field(default=None, compare=False, repr=False)


def _folder_forms(value: Any) -> tuple[str, ...]:
    """Return a folder as written and as resolved, or nothing if it is not absolute."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return ()
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        return ()
    if not path.is_absolute():
        return ()
    written = os.path.normpath(str(path))
    try:
        resolved = os.path.realpath(written)
    except (OSError, ValueError):
        resolved = written
    return tuple(dict.fromkeys((written, resolved)))


def _room_folders(folders: Any) -> tuple[str, ...]:
    if not isinstance(folders, Mapping):
        return ()
    values: list[Any] = [folders.get("registry_folder")]
    repos = folders.get("registry_repos")
    values.extend(repos if isinstance(repos, list) else [])
    for name in _FOLDER_FIELDS:
        entries = folders.get(name)
        for entry in entries if isinstance(entries, list) else []:
            values.append(entry.get("path") if isinstance(entry, Mapping) else entry)
    found: dict[str, None] = {}
    for value in values:
        for form in _folder_forms(value):
            found[form] = None
    return tuple(found)


def _room_patterns(patterns: Any) -> tuple[tuple[tuple[re.Pattern[str], str], ...], tuple[tuple[re.Pattern[str], str], ...]]:
    strong: list[tuple[re.Pattern[str], str]] = []
    weak: list[tuple[re.Pattern[str], str]] = []
    for entry in patterns if isinstance(patterns, list) else []:
        if not isinstance(entry, Mapping):
            raise ValueError("pattern entry must be an object")
        signal, strength, source = entry.get("signal"), entry.get("strength"), entry.get("re")
        if signal not in _TEXT_SIGNALS:
            continue
        if strength not in {"strong", "weak"} or not isinstance(source, str) or not source:
            raise ValueError("pattern entry needs re, signal and strength")
        compiled = re.compile(source)
        (strong if strength == "strong" else weak).append((compiled, signal))
    return tuple(strong), tuple(weak)


def _room_people(people: Any) -> tuple[Person, ...]:
    found = []
    for entry in people if isinstance(people, list) else []:
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if isinstance(name, str) and name.strip():
            words = name.split()
            if words and words[0][:1].isupper():
                found.append(Person(name=" ".join(words), first=words[0]))
    return tuple(found)


def _first_name_owners(entries: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Map each first name to every room its people belong to, spans included.

    Every room in the list counts here, clients or not, so a first name that a
    client shares with anyone else in the list counts for no room.
    """
    owners: dict[str, set[str]] = {}
    for entry in entries:
        people = entry.get("people")
        for person in people if isinstance(people, list) else []:
            if not isinstance(person, Mapping):
                continue
            name = person.get("name")
            words = name.split() if isinstance(name, str) else []
            if not words or not words[0][:1].isupper():
                continue
            spans = person.get("spans")
            spanned = [item for item in (spans if isinstance(spans, list) else []) if isinstance(item, str)]
            rooms = {entry["id"], *spanned}
            owners.setdefault(words[0], set()).update(rooms)
    return owners


def _first_name_pattern(names: Sequence[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    # Whole words, case as written: a lowercase word or a longer word never matches.
    choices = "|".join(re.escape(name) for name in sorted(names, key=lambda item: (-len(item), item)))
    return re.compile(rf"(?<![\w-])(?:{choices})(?![\w-])")


def rooms_from_document(document: Any) -> tuple[Room, ...]:
    """Build the client rooms from a decoded client list.

    Any malformed room makes the whole list unusable, so it raises: a reader that
    half-understood the list could name the wrong client.
    """
    if not isinstance(document, Mapping) or not isinstance(document.get("rooms"), list):
        raise ValueError("client list must be an object with a rooms array")
    rooms: list[Room] = []
    seen: set[str] = set()
    for entry in document["rooms"]:
        if not isinstance(entry, Mapping):
            raise ValueError("room must be an object")
        room_id = entry.get("id")
        if not isinstance(room_id, str) or not room_id or room_id in seen:
            raise ValueError("room needs a unique id")
        seen.add(room_id)
    owners = _first_name_owners(document["rooms"])
    for entry in document["rooms"]:
        room_id = entry["id"]
        if entry.get("kind") not in _CLIENT_ROOM_KINDS:
            continue
        prefixes = entry.get("thread_title_prefixes")
        prefixes = tuple(
            item for item in (prefixes if isinstance(prefixes, list) else [])
            if isinstance(item, str) and item.strip()
        )
        strong, weak = _room_patterns(entry.get("name_patterns"))
        people = _room_people(entry.get("people"))
        first_names = tuple(sorted({
            person.first for person in people if owners.get(person.first) == {room_id}
        }))
        rooms.append(Room(
            room_id=room_id, prefixes=prefixes, folders=_room_folders(entry.get("folders")),
            strong=strong, weak=weak, people=people, first_names=first_names,
            first_name_re=_first_name_pattern(first_names),
        ))
    return tuple(rooms)


def load_rooms(path: Path | str | None) -> tuple[Room, ...]:
    """Read the client list. A list that cannot be read or parsed names no client."""
    if path is None:
        return ()
    try:
        with open(Path(path).expanduser(), encoding="utf-8") as handle:
            return rooms_from_document(json.load(handle))
    except (OSError, ValueError, TypeError, RecursionError, re.error, RuntimeError):
        return ()


# ---------------------------------------------------------------- signals


@dataclass(frozen=True)
class Signal:
    kind: str
    room: str
    detail: str
    strength: str = "strong"

    def as_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "kind": self.kind, "room": self.room, "strength": self.strength}


def folder_signals(cwd: str | None, rooms: Sequence[Room]) -> list[Signal]:
    """Name the rooms whose folder holds the starting folder most closely.

    Only the deepest matching folder counts, so a client's folder inside a shared
    one names that client. Two rooms tied at that depth each get a signal, which
    makes the answer unknown.
    """
    forms = _folder_forms(cwd)
    if not forms:
        return []
    best: dict[str, int] = {}
    for room in rooms:
        for folder in room.folders:
            for form in forms:
                if form == folder or form.startswith(folder.rstrip(os.sep) + os.sep):
                    best[room.room_id] = max(best.get(room.room_id, -1), len(folder))
    if not best:
        return []
    deepest = max(best.values())
    return [Signal("folder", room, "starting_folder") for room, depth in best.items() if depth == deepest]


def thread_name_signals(name: str | None, rooms: Sequence[Room]) -> list[Signal]:
    """Name each room one of whose prefixes starts the thread's name."""
    if not isinstance(name, str) or not name.strip():
        return []
    title = name.lstrip()
    return [
        Signal("thread_name", room.room_id, "prefix")
        for room in rooms if any(title.startswith(prefix) for prefix in room.prefixes)
    ]


def named_signals(text: str, rooms: Sequence[Room]) -> list[Signal]:
    """Name each room a strong pattern finds in the text, and note weak ones.

    One signal per room and strength, whatever matched; the detail says what kind
    of pattern it was. Weak entries are evidence for the card and never count.
    """
    scanned = text[:SCAN_CHARS]
    found: list[Signal] = []
    for room in rooms:
        for strength, patterns in (("strong", room.strong), ("weak", room.weak)):
            kinds = sorted({signal for pattern, signal in patterns if pattern.search(scanned)})
            if kinds:
                found.append(Signal("named", room.room_id, "+".join(kinds), strength))
    return found


def first_name_signals(text: str, rooms: Sequence[Room]) -> list[Signal]:
    """Name each room one of whose own first names stands alone in the text.

    A first name that is part of a name, domain or address some room's pattern
    already matched is not counted again, so one full name is never two signals.
    """
    scanned = text[:SCAN_CHARS]
    if not any(room.first_name_re is not None for room in rooms):
        return []
    masked = list(scanned)
    for room in rooms:
        for pattern, _signal in (*room.strong, *room.weak):
            for match in pattern.finditer(scanned):
                masked[match.start():match.end()] = " " * (match.end() - match.start())
    remaining = "".join(masked)
    return [
        Signal("first_name", room.room_id, "person_first_name", BACKING)
        for room in rooms if room.first_name_re is not None and room.first_name_re.search(remaining)
    ]


@dataclass(frozen=True)
class ClientVerdict:
    status: str
    guess: str | None
    reason: str


def client_verdict(signals: Iterable[Signal]) -> ClientVerdict:
    """Apply the two-signal rule.

    Strong signals of two kinds naming one client mark client work for it. A
    backing signal (a first name) is a second kind only beside a strong signal for
    the same client, and makes nothing on its own. One kind marks the client
    unknown. Signals naming different clients, a first name included, mark it
    unknown, and no client is picked. Weak signals never count.
    """
    signals = list(signals)
    strong = [signal for signal in signals if signal.strength == "strong"]
    if not strong:
        return ClientVerdict(NONE, None, "no_signal")
    counted = strong + [signal for signal in signals if signal.strength == BACKING]
    named_rooms = {signal.room for signal in counted}
    if len(named_rooms) > 1:
        return ClientVerdict(UNKNOWN, None, "signals_disagree")
    kinds = {signal.kind for signal in counted}
    if len(kinds) >= 2:
        return ClientVerdict(CLIENT, next(iter(named_rooms)), "signals_agree")
    return ClientVerdict(UNKNOWN, None, "one_signal")


def card_asks(verdict: ClientVerdict, shape: Shape) -> list[str]:
    """What the card must ask about a row: the client, the speaker, both or nothing."""
    asks = []
    if verdict.status == UNKNOWN:
        asks.append(ASK_CLIENT)
    if not shape.flags:
        asks.append(ASK_SPEAKER)
    return sorted(asks)


# ---------------------------------------------------------------- shape


@dataclass(frozen=True)
class Shape:
    flags: tuple[str, ...]
    loom_ids: tuple[str, ...]
    image_paths: tuple[str, ...]
    timestamp_lines: int
    lead_line: str | None = field(default=None, repr=False)
    slack_names: tuple[str, ...] = field(default=(), repr=False)


def request_body(text: str) -> str:
    """Return what the person wrote, without Codex's header listing the files.

    Codex Desktop puts mentioned and pasted files in a header, then the request
    under a ``## My request`` line.
    """
    if not _FILES_HEADER.match(text):
        return text
    requests = list(_REQUEST_LINE.finditer(text))
    return text[requests[-1].end():] if requests else ""


def _header_images(text: str) -> list[str]:
    if not _FILES_HEADER.match(text):
        return []
    requests = list(_REQUEST_LINE.finditer(text))
    header = text[:requests[-1].start()] if requests else text
    return [
        path for path in (match.group(1) for match in _HEADER_FILE.finditer(header))
        if path.lower().endswith(_IMAGE_SUFFIXES)
    ]


def paste_shape(text: str, *, image_count: int = 0) -> Shape:
    """Find the marks of pasted feedback in a prompt. None of them names a client."""
    scanned = text[:SCAN_CHARS]
    body = request_body(scanned)
    lines = body.splitlines()
    content = [line for line in lines if line.strip()]
    flags: set[str] = set()
    lead = None
    if len(content) >= 2:
        first = content[0].rstrip()
        if len(first) <= _LEAD_CHARS and first.endswith(":") and not first.lstrip().startswith("#"):
            flags.add("colon_lead")
            lead = first
    if any(_RULE_RUN.match(line) for line in lines):
        flags.add("rule_run")
    stamps = sum(1 for line in lines if _TIMESTAMP_LINE.match(line))
    if stamps >= TIMESTAMP_RUN:
        flags.add("timestamp_lines")
    slack_names = []
    for line in lines:
        for pattern in _SLACK_LINES:
            match = pattern.search(line)
            if match:
                slack_names.append(match.group(1))
                break
    if slack_names:
        flags.add("slack_name_time")
    images = list(dict.fromkeys([*_IMAGE_TAG.findall(scanned), *_header_images(scanned)]))
    if images or image_count > 0:
        flags.add("image_attachment")
    looms = tuple(dict.fromkeys(match.lower() for match in _LOOM.findall(scanned)))
    return Shape(
        flags=tuple(flag for flag in SHAPE_FLAGS if flag in flags), loom_ids=looms,
        image_paths=tuple(images), timestamp_lines=stamps, lead_line=lead,
        slack_names=tuple(slack_names),
    )


def carrier_kinds(shape: Shape) -> list[str]:
    """What carried the feedback in this prompt. Empty means typed words only."""
    kinds = []
    if _TEXT_SHAPES.intersection(shape.flags):
        kinds.append("paste")
    if "image_attachment" in shape.flags:
        kinds.append("image")
    if shape.loom_ids:
        kinds.append("loom_link")
    return kinds


def speaker_guess(shape: Shape, rooms: Sequence[Room], candidates: Iterable[str]) -> dict[str, str] | None:
    """Name the client person a Slack name line or the lead line names, if exactly one.

    Only people of the candidate rooms are looked for, so a first name here never
    picks a room. A Slack line outranks the lead line.
    """
    wanted = set(candidates)
    people = [(room.room_id, person) for room in rooms if room.room_id in wanted for person in room.people]
    for basis, lines in (("slack_line", shape.slack_names), ("lead_line", (shape.lead_line,) if shape.lead_line else ())):
        hits = {
            (room_id, person.name) for room_id, person in people for line in lines
            if re.search(rf"(?<![\w-]){re.escape(person.name)}(?![\w-])", line)
            or re.search(rf"(?<![\w-]){re.escape(person.first)}(?![\w-])", line)
        }
        if len(hits) == 1:
            room_id, name = next(iter(hits))
            return {"basis": basis, "person": name, "room": room_id}
        if hits:
            return None
    return None


# ---------------------------------------------------------------- origin


@dataclass(frozen=True)
class ThreadRecord:
    thread_id: str
    name: str | None
    source: str | None
    thread_source: str | None
    spawned: bool
    agent_marked: bool


_THREAD_QUERY = (
    "SELECT name, source, thread_source, agent_nickname, agent_role, agent_path, "
    "attached_thread_parent_id FROM threads WHERE id = ?"
)
_SPAWN_QUERY = "SELECT 1 FROM thread_spawn_edges WHERE child_thread_id = ? LIMIT 1"


def codex_state_path(environ: Mapping[str, str] | None = None) -> Path | None:
    values = os.environ if environ is None else environ
    home = values.get(CODEX_HOME_ENV, "").strip()
    try:
        base = Path(home).expanduser() if home else Path.home() / ".codex"
    except RuntimeError:
        return None
    return base / CODEX_STATE_FILE


def codex_thread_record(database: Path | str | None, thread_id: str | None) -> ThreadRecord | None:
    """Read one thread's own record, read-only. Any failure returns None.

    The database is opened by URI with ``mode=ro`` and queried only, with a short
    wait for a lock, so this can never write it or hold a prompt for long. Codex
    keeps it in write-ahead-log mode. While Codex is running, as it is for every
    hook call, the read creates no file. With no writer present, SQLite itself
    may create the empty ``-wal`` and ``-shm`` files beside it, as on a hand run.
    """
    if database is None or not isinstance(thread_id, str) or not thread_id:
        return None
    connection = None
    try:
        path = Path(database)
        if not path.is_file():
            return None
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS,
        )
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(_THREAD_QUERY, (thread_id,)).fetchone()
        if row is None:
            return None
        spawned = connection.execute(_SPAWN_QUERY, (thread_id,)).fetchone() is not None
    except (sqlite3.Error, OSError, ValueError, RuntimeError):
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    name, source, thread_source, nickname, role, agent_path, attached = row
    return ThreadRecord(
        thread_id=thread_id,
        name=name if isinstance(name, str) else None,
        source=source if isinstance(source, str) else None,
        thread_source=thread_source if isinstance(thread_source, str) else None,
        spawned=spawned,
        agent_marked=any(value not in (None, "") for value in (nickname, role, agent_path, attached)),
    )


def person_started(record: ThreadRecord | None) -> bool:
    """Whether Codex records this thread as one a person started.

    Its own source says a user started it from an interactive surface, no spawn
    edge names it as a child, and no agent field is set. Anything else, an
    unreadable record included, is not.
    """
    return (
        record is not None
        and record.thread_source == _PERSON_THREAD_SOURCE
        and record.source in _PERSON_SOURCES
        and not record.spawned
        and not record.agent_marked
    )


def machine_prompt(text: str) -> bool:
    """Whether a prompt is harness or automation text rather than a person's words."""
    return text.lstrip().startswith(MACHINE_PREFIXES)


def subagent_event(event: Mapping[str, Any]) -> bool:
    return any(event.get(name) not in (None, "") for name in _SUBAGENT_FIELDS)


# ---------------------------------------------------------------- the row


@dataclass(frozen=True)
class Reading:
    signals: tuple[Signal, ...]
    verdict: ClientVerdict
    shape: Shape


def read_prompt(text: str, *, rooms: Sequence[Room], cwd: str | None, thread_name: str | None,
                image_count: int = 0) -> Reading:
    """Read one prompt's client signals and paste shape. Pure."""
    signals = (
        *folder_signals(cwd, rooms), *thread_name_signals(thread_name, rooms), *named_signals(text, rooms),
        *first_name_signals(text, rooms),
    )
    return Reading(signals=signals, verdict=client_verdict(signals), shape=paste_shape(text, image_count=image_count))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()


def carrier_row(
    text: str, *, rooms: Sequence[Room], person_started: bool, thread_id: str,
    message_id: str | None, time: str, cwd: str | None, thread_name: str | None,
    image_count: int = 0, channel: str = CHANNEL_CODEX,
) -> dict[str, Any] | None:
    """Build the carrier row for one prompt, or None when it makes no row. Pure.

    No row comes from a thread a person did not start, from harness or automation
    text, or from a prompt no signal marks as client work.
    """
    if not person_started or not isinstance(text, str) or not text.strip() or machine_prompt(text):
        return None
    reading = read_prompt(text, rooms=rooms, cwd=cwd, thread_name=thread_name, image_count=image_count)
    verdict = reading.verdict
    if verdict.status == NONE:
        return None
    # A speaker is looked for only in a client the signals agree on, so it never
    # hints at a client the row leaves unknown.
    candidates = [verdict.guess] if verdict.guess else []
    shape = reading.shape
    return {
        "schema": ROW_SCHEMA,
        "thread_id": thread_id,
        "message_id": message_id,
        "text_sha256": text_hash(text),
        "time": time,
        "shape_flags": list(shape.flags),
        "client": {
            "status": verdict.status,
            "guess": verdict.guess,
            "reason": verdict.reason,
            "signals": [signal.as_dict() for signal in sorted(
                reading.signals, key=lambda item: (item.kind, item.room, item.strength),
            )],
        },
        "asks": card_asks(verdict, shape),
        "loom_ids": list(shape.loom_ids),
        "image_paths": list(shape.image_paths),
        "speaker_guess": speaker_guess(shape, rooms, candidates),
        "channel": channel,
        "carrier": carrier_kinds(shape),
        # Rounds are numbered by date and artifact across rows, which a single
        # prompt cannot see; the batched reader numbers them.
        "round": None,
    }


# ---------------------------------------------------------------- the queue


def _inside(path: str, folder: str | None) -> bool:
    if not folder:
        return False
    for form in _folder_forms(folder):
        if path == form or path.startswith(form.rstrip(os.sep) + os.sep):
            return True
    return False


def checked_queue_path(
    value: Any, *, cwd: str | None, forbidden: Iterable[str | None] = (),
) -> Path | None:
    """Return the queue file to append to, or None when it may not be used.

    It must be an absolute path that is not a link, outside the session's
    starting folder, outside every forbidden folder (the store's root, the host's
    session folders), outside every git repository, and not in a cloud-synced
    folder, which includes the home ``Documents`` and ``Desktop`` folders.
    """
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        return None
    if not path.is_absolute() or not cwd:
        return None
    resolved = os.path.realpath(os.path.normpath(str(path)))
    if any(marker.lower() in resolved.lower() for marker in _SYNC_MARKERS):
        return None
    for folder in (cwd, *forbidden, *_synced_home_folders()):
        if _inside(resolved, folder):
            return None
    if _inside_a_repository(resolved):
        return None
    if os.path.islink(str(path)):
        return None
    return Path(resolved)


def append_row(path: Path, row: Mapping[str, Any]) -> None:
    """Append one row as one line, creating a private file. Raises on failure."""
    line = (json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    parent = path.parent
    if not parent.is_dir():
        from imprint.permissions import secure_directory

        secure_directory(parent)
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("queue is not a regular file")
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def codex_carrier_step(
    event: Any, config: Mapping[str, Any], *, operator_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None, now: str | None = None,
) -> bool:
    """Append the carrier row for one Codex prompt event. Never raises.

    Returns whether a row was written. Nothing is read beyond the config until the
    queue key is set, and nothing at all is written unless every check passes.
    """
    try:
        queue_value = config.get(QUEUE_KEY)
        if queue_value is None or not isinstance(event, Mapping):
            return False
        if event.get("hook_event_name") not in (None, "UserPromptSubmit") or subagent_event(event):
            return False
        text = event.get("prompt")
        thread_id = event.get("session_id")
        cwd = event.get("cwd")
        if not isinstance(text, str) or not isinstance(thread_id, str) or not isinstance(cwd, str):
            return False
        if machine_prompt(text) or not text.strip():
            return False
        rooms = load_rooms(config.get(CLIENTS_KEY))
        if not rooms:
            return False
        state = codex_state_path(environ)
        record = codex_thread_record(state, thread_id)
        if not person_started(record):
            return False
        turn = event.get("turn_id")
        row = carrier_row(
            text, rooms=rooms, person_started=True, thread_id=thread_id,
            message_id=turn if isinstance(turn, str) and turn else None,
            time=now or _stamp(), cwd=cwd, thread_name=record.name,
        )
        if row is None:
            return False
        transcript = event.get("transcript_path")
        forbidden = [
            str(operator_root) if operator_root is not None else None,
            str(state.parent) if state is not None else None,
            os.path.dirname(transcript) if isinstance(transcript, str) and transcript else None,
        ]
        queue = checked_queue_path(queue_value, cwd=cwd, forbidden=forbidden)
        if queue is None:
            return False
        append_row(queue, row)
        return True
    except Exception:
        return False


def _read_event(stream: Any) -> bytes | None:
    """Read the whole event, keeping at most the limit, so the host's write never fails."""
    source = getattr(stream, "buffer", stream)
    kept = bytearray()
    oversized = False
    while True:
        chunk = source.read(1 << 16)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "replace")
        if not oversized:
            kept += chunk
            if len(kept) > MAX_EVENT_BYTES:
                oversized = True
                kept = bytearray()
    return None if oversized else bytes(kept)


def run_carrier_step(host: str, stream: Any, *, config_path: Path | None = None) -> int:
    """The ``imprint carrier`` command: read one event, maybe append a row, exit 0.

    It prints nothing, so the agent sees nothing, and it never fails the prompt.
    """
    try:
        raw = _read_event(stream)
        if host != "codex" or raw is None:
            return 0
        from imprint.config import load_config, resolved_operator_root

        config = load_config(config_path)
        if config.get(QUEUE_KEY) is None:
            return 0
        event = json.loads(raw.decode("utf-8", "replace"))
        codex_carrier_step(event, config, operator_root=resolved_operator_root(config))
    except BaseException:  # noqa: BLE001 - a hook that exits non-zero could block the prompt
        return 0
    return 0


__all__ = [
    "ASK_CLIENT",
    "ASK_SPEAKER",
    "BACKING",
    "CLIENT",
    "CLIENTS_KEY",
    "CHANNEL_CODEX",
    "ClientVerdict",
    "NONE",
    "QUEUE_KEY",
    "ROW_SCHEMA",
    "Reading",
    "Room",
    "Shape",
    "Signal",
    "ThreadRecord",
    "UNKNOWN",
    "append_row",
    "carrier_kinds",
    "card_asks",
    "carrier_row",
    "checked_queue_path",
    "client_verdict",
    "codex_carrier_step",
    "codex_state_path",
    "codex_thread_record",
    "first_name_signals",
    "folder_signals",
    "load_rooms",
    "machine_prompt",
    "named_signals",
    "paste_shape",
    "person_started",
    "read_prompt",
    "request_body",
    "rooms_from_document",
    "run_carrier_step",
    "speaker_guess",
    "subagent_event",
    "text_hash",
    "thread_name_signals",
]
