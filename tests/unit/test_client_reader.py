"""The client-work reader and carrier queue, on invented clients and invented text."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from imprint.capture import client_reader as reader
from imprint.config import CLIENT_CARRIER_CLIENTS, CLIENT_CARRIER_QUEUE, DEFAULTS, ValidationError, parse_config

NW_PERSON = "Mara Quill"
BF_PERSON = "Odo Vexley"
SECRET_WORD = "zephyrine"


def client_list(root: Path) -> dict:
    return {
        "schema": "imprint-clients/draft-1",
        "rooms": [
            {
                "id": "northwind",
                "kind": "client",
                "name_patterns": [
                    {"re": r"(?i)\bNorthwind Rowing\b", "signal": "room_name", "strength": "strong"},
                    {"re": r"(?i)\bnorthwind-rowing\.example\b", "signal": "domain", "strength": "strong"},
                    {"re": r"\bMara Quill\b", "signal": "person_full_name", "strength": "strong"},
                    {"re": r"(?i)\bthe boathouse\b", "signal": "product", "strength": "weak"},
                    {"re": r"^NW ", "signal": "prefix", "strength": "strong"},
                ],
                "people": [{"name": NW_PERSON, "spans": []}, {"name": "Tam Ivers", "spans": []}],
                "thread_title_prefixes": ["NW ·"],
                "folders": {
                    "registry_folder": str(root / "clients" / "northwind"),
                    "checkouts": [{"path": str(root / "desk" / "northwind-site")}],
                },
            },
            {
                "id": "bluefin",
                "kind": "client_rules_core_agents",
                "name_patterns": [
                    {"re": r"(?i)\bBluefin Labs\b", "signal": "room_name", "strength": "strong"},
                    {"re": r"\bOdo Vexley\b", "signal": "person_full_name", "strength": "strong"},
                ],
                "people": [{"name": BF_PERSON, "spans": []}],
                "thread_title_prefixes": ["BF ·"],
                "folders": {"registry_folder": str(root / "clients" / "bluefin")},
            },
            {
                "id": "sideproject",
                "kind": "own_venture",
                "name_patterns": [
                    {"re": r"(?i)\bKestrel Tea\b", "signal": "room_name", "strength": "strong"},
                ],
                "people": [],
                "thread_title_prefixes": ["KT ·"],
                "folders": {"registry_folder": str(root / "ventures" / "kestrel")},
            },
            {
                "id": "desk-a",
                "kind": "client",
                "name_patterns": [],
                "people": [],
                "thread_title_prefixes": [],
                "folders": {"registry_folder": str(root / "desk")},
            },
        ],
    }


@pytest.fixture
def rooms(tmp_path):
    return reader.rooms_from_document(client_list(tmp_path))


def row_for(text, rooms, *, thread_name=None, cwd="/nowhere", started=True, image_count=0):
    return reader.carrier_row(
        text, rooms=rooms, person_started=started, thread_id="thread-1", message_id="turn-1",
        time="2026-01-02T03:04:05Z", cwd=cwd, thread_name=thread_name, image_count=image_count,
    )


# ------------------------------------------------------------- client list


def test_own_ventures_are_not_clients(rooms):
    assert [room.room_id for room in rooms] == ["northwind", "bluefin", "desk-a"]
    assert reader.client_verdict(reader.named_signals("Kestrel Tea notes", rooms)).status == reader.NONE


@pytest.mark.parametrize("document", [
    [], {"rooms": {}}, {"rooms": [{"kind": "client"}]},
    {"rooms": [{"id": "a", "kind": "client"}, {"id": "a", "kind": "client"}]},
    {"rooms": [{"id": "a", "kind": "client", "name_patterns": [{"re": "(", "signal": "domain", "strength": "strong"}]}]},
    {"rooms": [{"id": "a", "kind": "client", "name_patterns": [{"re": "x", "signal": "domain", "strength": "loud"}]}]},
])
def test_a_malformed_list_names_no_client(tmp_path, document):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps(document))
    assert reader.load_rooms(path) == ()


def test_a_missing_or_unset_list_names_no_client(tmp_path):
    assert reader.load_rooms(None) == ()
    assert reader.load_rooms(tmp_path / "absent.json") == ()
    (tmp_path / "bad.json").write_text("{not json")
    assert reader.load_rooms(tmp_path / "bad.json") == ()


# ------------------------------------------------------------- the two-signal rule


def test_two_kinds_naming_one_client_mark_client_work(rooms):
    row = row_for(f"Notes from {NW_PERSON} on the draft", rooms, thread_name="NW · Draft two")
    assert row["client"]["status"] == "client"
    assert row["client"]["guess"] == "northwind"
    assert row["client"]["reason"] == "signals_agree"
    assert {item["kind"] for item in row["client"]["signals"]} == {"named", "thread_name"}


def test_folder_and_thread_name_agree(rooms, tmp_path):
    cwd = str(tmp_path / "clients" / "northwind" / "site")
    row = row_for("Please tighten the second paragraph", rooms, thread_name="NW · copy", cwd=cwd)
    assert (row["client"]["status"], row["client"]["guess"]) == ("client", "northwind")


@pytest.mark.parametrize("kind", ["folder", "thread_name", "named"])
def test_one_signal_leaves_the_client_unknown(rooms, tmp_path, kind):
    text = "Please tighten the second paragraph"
    options = {"cwd": "/nowhere", "thread_name": "Copy pass"}
    if kind == "folder":
        options["cwd"] = str(tmp_path / "clients" / "bluefin")
    elif kind == "thread_name":
        options["thread_name"] = "BF · copy pass"
    else:
        text = "Bluefin Labs wants the second paragraph tighter"
    row = row_for(text, rooms, **options)
    assert row["client"] == {**row["client"], "status": "unknown", "guess": None, "reason": "one_signal"}
    assert [item["kind"] for item in row["client"]["signals"]] == [kind]


def test_disagreeing_signals_leave_the_client_unknown(rooms):
    row = row_for(f"{BF_PERSON} sent notes", rooms, thread_name="NW · Draft two")
    assert row["client"]["status"] == "unknown"
    assert row["client"]["guess"] is None
    assert row["client"]["reason"] == "signals_disagree"
    assert {item["room"] for item in row["client"]["signals"]} == {"bluefin", "northwind"}


def test_the_same_kind_twice_is_still_one_signal(rooms):
    row = row_for(f"{NW_PERSON} at northwind-rowing.example wrote back", rooms)
    assert row["client"]["reason"] == "one_signal"
    assert row["client"]["signals"] == [
        {"detail": "domain+person_full_name", "kind": "named", "room": "northwind", "strength": "strong"},
    ]


def test_no_signal_makes_no_row(rooms):
    assert row_for("Please tighten the second paragraph", rooms, thread_name="Copy pass") is None


def test_a_weak_pattern_never_counts(rooms):
    assert row_for("Back to the boathouse page", rooms) is None
    row = row_for("Back to the boathouse page", rooms, thread_name="NW · pages")
    assert row["client"]["reason"] == "one_signal"
    assert {"detail": "product", "kind": "named", "room": "northwind", "strength": "weak"} in row["client"]["signals"]


FIRST_NAME_BACKING = {"detail": "person_first_name", "kind": "first_name", "room": "northwind", "strength": "backing"}


def test_a_first_name_alone_gives_no_client(rooms):
    assert row_for("Mara said the second paragraph is too long", rooms) is None
    # A weak pattern does not make it count either.
    assert row_for("Mara said the boathouse page is too long", rooms) is None
    assert reader.client_verdict(reader.first_name_signals("Mara and Tam", rooms)).status == reader.NONE


def test_a_first_name_backs_a_prefix_for_the_same_client(rooms):
    row = row_for("Mara said the second paragraph is too long", rooms, thread_name="NW · pages")
    assert (row["client"]["status"], row["client"]["guess"], row["client"]["reason"]) == (
        "client", "northwind", "signals_agree",
    )
    assert FIRST_NAME_BACKING in row["client"]["signals"]
    assert row["asks"] == ["speaker"]


def test_a_first_name_backs_a_domain_or_a_folder(rooms, tmp_path):
    row = row_for("Mara wrote from northwind-rowing.example", rooms)
    assert (row["client"]["status"], row["client"]["guess"]) == ("client", "northwind")
    row = row_for("Mara wants it shorter", rooms, cwd=str(tmp_path / "clients" / "northwind"))
    assert (row["client"]["status"], row["client"]["guess"]) == ("client", "northwind")


def test_a_first_name_of_another_client_makes_it_unknown(rooms):
    row = row_for("Mara said the second paragraph is too long", rooms, thread_name="BF · pages")
    assert row["client"]["status"] == "unknown"
    assert row["client"]["guess"] is None
    assert row["client"]["reason"] == "signals_disagree"
    assert row["asks"] == ["client", "speaker"]


def test_a_full_name_is_not_also_counted_as_a_first_name(rooms):
    row = row_for(f"{NW_PERSON} said the second paragraph is too long", rooms)
    assert row["client"]["reason"] == "one_signal"
    assert reader.first_name_signals(f"{NW_PERSON} said so", rooms) == []
    assert reader.first_name_signals("ask mara@northwind-rowing.example", rooms) == []


@pytest.mark.parametrize("person", [
    {"room": 1, "entry": {"name": "Tam Rook"}},
    {"room": 2, "entry": {"name": "Tam Ash"}},
    {"room": 0, "entry": {"name": "Tam Ivers", "spans": ["bluefin"]}},
])
def test_a_first_name_held_by_two_rooms_counts_for_neither(tmp_path, person):
    document = client_list(tmp_path)
    people = document["rooms"][person["room"]]["people"]
    if person["room"] == 0:
        people[1] = person["entry"]
    else:
        people.append(person["entry"])
    rooms = reader.rooms_from_document(document)
    assert "Tam" not in next(room for room in rooms if room.room_id == "northwind").first_names
    assert reader.first_name_signals("Tam said so", rooms) == []
    row = row_for("Tam said the second paragraph is too long", rooms, thread_name="NW · pages")
    assert row["client"]["reason"] == "one_signal"


@pytest.mark.parametrize("text,counts", [
    ("Mara's notes are in", True), ("mara said so", False), ("Maradona said so", False),
    ("Mara-Lee said so", False), ("see (Mara) above", True),
])
def test_a_first_name_matches_as_a_whole_word_as_written(rooms, text, counts):
    assert bool(reader.first_name_signals(text, rooms)) is counts


@pytest.mark.parametrize("text,thread_name,images,status,asks", [
    ("Here is the note:\nfirst point", "NW · pages", 0, "unknown", ["client"]),
    (f"{NW_PERSON} sent this:\nfirst point", "NW · pages", 0, "client", []),
    (f"{NW_PERSON} thinks it is too long", "NW · pages", 0, "client", ["speaker"]),
    ("Please tighten the second paragraph", "NW · pages", 0, "unknown", ["client", "speaker"]),
    ("Please tighten the second paragraph", "NW · pages", 1, "unknown", ["client"]),
])
def test_the_row_says_what_the_card_must_ask(rooms, text, thread_name, images, status, asks):
    row = row_for(text, rooms, thread_name=thread_name, image_count=images)
    assert row["client"]["status"] == status
    assert row["asks"] == asks


def test_a_prefix_counts_only_at_the_start_of_the_name(rooms):
    assert reader.thread_name_signals("Re: NW · pages", rooms) == []
    assert [s.room for s in reader.thread_name_signals("  NW · pages", rooms)] == ["northwind"]
    assert reader.thread_name_signals(None, rooms) == []


def test_the_deepest_folder_wins(rooms, tmp_path):
    shared = reader.folder_signals(str(tmp_path / "desk" / "notes"), rooms)
    assert [s.room for s in shared] == ["desk-a"]
    inner = reader.folder_signals(str(tmp_path / "desk" / "northwind-site" / "src"), rooms)
    assert [s.room for s in inner] == ["northwind"]


def test_rooms_tied_on_one_folder_leave_the_client_unknown(tmp_path):
    document = client_list(tmp_path)
    document["rooms"][1]["folders"]["registry_folder"] = str(tmp_path / "clients" / "northwind")
    rooms = reader.rooms_from_document(document)
    signals = reader.folder_signals(str(tmp_path / "clients" / "northwind"), rooms)
    assert {s.room for s in signals} == {"northwind", "bluefin"}
    assert reader.client_verdict(signals).reason == "signals_disagree"


def test_a_folder_matches_whole_path_parts_only(rooms, tmp_path):
    assert reader.folder_signals(str(tmp_path / "clients" / "northwind-old"), rooms) == []
    assert reader.folder_signals("relative/clients/northwind", rooms) == []
    assert reader.folder_signals(None, rooms) == []


def test_paste_shape_is_never_a_client_signal(rooms):
    pasted = "Notes from the call:\n====\n0:00 one\n0:10 two\n0:20 three\nSam Ray  9:14 AM\n"
    reading = reader.read_prompt(pasted, rooms=rooms, cwd=None, thread_name=None, image_count=2)
    assert set(reading.shape.flags) == set(reader.SHAPE_FLAGS)
    assert reading.signals == ()
    assert row_for(pasted, rooms, image_count=2) is None


# ------------------------------------------------------------- paste shape


def test_a_colon_lead_needs_a_short_lead_and_more_lines():
    assert "colon_lead" in reader.paste_shape("Here is what they said:\nThe first point").flags
    assert "colon_lead" not in reader.paste_shape("Here is what they said:").flags
    assert "colon_lead" not in reader.paste_shape("## Heading:\nbody").flags
    assert "colon_lead" not in reader.paste_shape(("word " * 70) + ":\nbody").flags


def test_a_rule_run_needs_four_equals_signs_alone_on_a_line():
    assert "rule_run" in reader.paste_shape("lead\n  ======  \nbody").flags
    assert "rule_run" not in reader.paste_shape("lead\n===\nbody").flags
    assert "rule_run" not in reader.paste_shape("a ==== b").flags


def test_timestamp_lines_need_three():
    three = "0:00 hello\n[0:15] next\n(1:02:03) last"
    assert "timestamp_lines" in reader.paste_shape(three).flags
    assert reader.paste_shape(three).timestamp_lines == 3
    assert "timestamp_lines" not in reader.paste_shape("0:00 hello\n0:15 next").flags


@pytest.mark.parametrize("line,name", [
    ("Sam Ray  9:14 AM", "Sam Ray"),
    ("Sam Ray [9:14 AM]", "Sam Ray"),
    ("great, ship it -- Sam Ray  [9:14 AM]", "Sam Ray"),
    ("[9:14 PM] Sam Ray: ship it", "Sam Ray"),
])
def test_slack_name_and_time_lines(line, name):
    shape = reader.paste_shape(f"first\n{line}\nlast")
    assert "slack_name_time" in shape.flags
    assert shape.slack_names == (name,)


def test_ordinary_times_are_not_slack_lines():
    assert "slack_name_time" not in reader.paste_shape("meet at 9:14 AM tomorrow\nsee you").flags


def test_the_files_header_is_not_the_request():
    text = (
        "# Files mentioned by the user:\n\n## shot.png: /tmp/invented/shot.png\n\n"
        "## My request:\nThe client wrote:\nfirst point\nsecond point"
    )
    assert reader.request_body(text) == "\nThe client wrote:\nfirst point\nsecond point"
    shape = reader.paste_shape(text)
    assert shape.flags == ("colon_lead", "image_attachment")
    assert shape.image_paths == ("/tmp/invented/shot.png",)
    assert reader.request_body("# Files mentioned by the user:\n## a.txt: /x/a.txt") == ""


def test_image_tags_and_attached_images_are_flagged():
    shape = reader.paste_shape('look <image name=[Image #1] path="/tmp/invented/a.png"> here')
    assert shape.image_paths == ("/tmp/invented/a.png",)
    assert reader.paste_shape("look here", image_count=1).flags == ("image_attachment",)
    assert reader.paste_shape("look here").flags == ()


def test_loom_ids_are_kept_lowercase_and_once():
    loom = "0123456789abcdef0123456789ABCDEF"
    text = f"https://www.loom.com/share/{loom} and loom.com/embed/{loom.lower()} and loom.com/share/123"
    shape = reader.paste_shape(text)
    assert shape.loom_ids == (loom.lower(),)
    assert reader.carrier_kinds(shape) == ["loom_link"]


def test_carrier_kinds():
    assert reader.carrier_kinds(reader.paste_shape("lead:\nbody", image_count=1)) == ["paste", "image"]
    assert reader.carrier_kinds(reader.paste_shape("typed words only")) == []


# ------------------------------------------------------------- speaker guess


def test_the_speaker_is_guessed_only_for_a_known_client(rooms):
    text = f"From {NW_PERSON}'s team:\nMara Quill  9:14 AM\nthe header is too big"
    known = row_for(text, rooms, thread_name="NW · pages")
    assert known["speaker_guess"] == {"basis": "slack_line", "person": NW_PERSON, "room": "northwind"}
    unknown = row_for(text, rooms, thread_name="BF · pages")
    assert unknown["client"]["status"] == "unknown"
    assert unknown["speaker_guess"] is None


def test_the_lead_line_names_the_speaker_when_no_slack_line_does(rooms):
    shape = reader.paste_shape("Tam sent this:\nthe header is too big")
    assert reader.speaker_guess(shape, rooms, ["northwind"]) == {
        "basis": "lead_line", "person": "Tam Ivers", "room": "northwind",
    }
    assert reader.speaker_guess(shape, rooms, ["bluefin"]) is None


def test_two_people_on_the_lines_name_no_speaker(rooms):
    shape = reader.paste_shape("Mara and Tam sent this:\nthe header is too big")
    assert reader.speaker_guess(shape, rooms, ["northwind"]) is None


# ------------------------------------------------------------- origin


def make_state(path: Path, rows: list[tuple], edges: list[tuple] = ()) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, source TEXT, thread_source TEXT, "
        "agent_nickname TEXT, agent_role TEXT, agent_path TEXT, attached_thread_parent_id TEXT)"
    )
    connection.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT)")
    connection.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)", rows)
    connection.executemany("INSERT INTO thread_spawn_edges VALUES (?,?,?)", edges)
    connection.commit()
    connection.close()
    return path


PERSON_ROW = ("t-person", "NW · pages", "vscode", "user", None, None, None, None)


@pytest.mark.parametrize("values,started", [
    (("t", None, "vscode", "user", None, None, None, None), True),
    (("t", None, "cli", "user", None, None, None, None), True),
    (("t", None, "exec", "user", None, None, None, None), False),
    (("t", None, '{"subagent":{}}', "subagent", None, None, None, None), False),
    (("t", None, "vscode", "automation", None, None, None, None), False),
    (("t", None, "vscode", "agent_created_thread", None, None, None, None), False),
    (("t", None, "vscode", None, None, None, None, None), False),
    (("t", None, "vscode", "user", "Scout", None, None, None), False),
    (("t", None, "vscode", "user", None, "worker", None, None), False),
    (("t", None, "vscode", "user", None, None, None, "t-parent"), False),
])
def test_only_threads_a_person_started_are_admitted(tmp_path, values, started):
    state = make_state(tmp_path / "state.sqlite", [values])
    assert reader.person_started(reader.codex_thread_record(state, "t")) is started


def test_a_spawned_thread_is_not_a_persons(tmp_path):
    state = make_state(tmp_path / "state.sqlite", [PERSON_ROW], [("t-parent", "t-person", "open")])
    record = reader.codex_thread_record(state, "t-person")
    assert record.spawned and not reader.person_started(record)


def test_the_thread_record_carries_its_name(tmp_path):
    state = make_state(tmp_path / "state.sqlite", [PERSON_ROW])
    record = reader.codex_thread_record(state, "t-person")
    assert record.name == "NW · pages" and reader.person_started(record)
    assert reader.codex_thread_record(state, "t-other") is None


def test_an_unreadable_state_gives_no_record(tmp_path):
    assert reader.codex_thread_record(tmp_path / "absent.sqlite", "t") is None
    assert reader.codex_thread_record(None, "t") is None
    (tmp_path / "junk.sqlite").write_bytes(b"not a database at all" * 100)
    assert reader.codex_thread_record(tmp_path / "junk.sqlite", "t") is None
    old = sqlite3.connect(tmp_path / "old.sqlite")
    old.execute("CREATE TABLE threads (id TEXT, name TEXT)")
    old.execute("INSERT INTO threads VALUES ('t', 'x')")
    old.commit()
    old.close()
    assert reader.codex_thread_record(tmp_path / "old.sqlite", "t") is None
    assert not reader.person_started(None)


def test_a_locked_state_gives_no_record_quickly(tmp_path):
    state = make_state(tmp_path / "state.sqlite", [PERSON_ROW])
    holder = sqlite3.connect(state, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        assert reader.codex_thread_record(state, "t-person") is None
        assert time.monotonic() - started < 5
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_reading_the_state_never_writes_it(tmp_path):
    folder = tmp_path / "codex"
    folder.mkdir()
    state = make_state(folder / "state.sqlite", [PERSON_ROW])
    before = (hashlib.sha256(state.read_bytes()).hexdigest(), state.stat().st_mtime_ns, sorted(os.listdir(folder)))
    for _ in range(3):
        assert reader.codex_thread_record(state, "t-person") is not None
    after = (hashlib.sha256(state.read_bytes()).hexdigest(), state.stat().st_mtime_ns, sorted(os.listdir(folder)))
    assert before == after


def test_codex_state_path_follows_codex_home(tmp_path):
    assert reader.codex_state_path({"CODEX_HOME": str(tmp_path)}) == tmp_path / "state_5.sqlite"
    assert reader.codex_state_path({"HOME": str(tmp_path)}).name == "state_5.sqlite"


@pytest.mark.parametrize("text", [
    "<environment_context>\n<cwd>/x</cwd>", "  <heartbeat/>", "<user_instructions>x",
    "<external_codex_apps_open_page>", "<subagent_notification>done", "<turn_aborted>",
    "# AGENTS.md instructions for /x",
])
def test_harness_and_automation_text_makes_no_row(rooms, text):
    assert reader.machine_prompt(text)
    assert row_for(f"{text}\n{NW_PERSON}", rooms, thread_name="NW · pages") is None


def test_a_thread_no_person_started_makes_no_row(rooms):
    assert row_for(f"{NW_PERSON} wrote:\nnotes", rooms, thread_name="NW · x", started=False) is None


def test_subagent_payload_fields():
    assert reader.subagent_event({"agent_id": "a1"})
    assert reader.subagent_event({"parent_session_id": "p"})
    assert not reader.subagent_event({"agent_id": None, "agent_type": ""})


# ------------------------------------------------------------- the row


def test_the_row_is_a_pointer_with_no_text(rooms):
    loom = "a" * 32
    text = (
        f"{NW_PERSON} sent this {SECRET_WORD}:\n====\n0:00 one\n0:10 two\n0:20 three\n"
        f"https://loom.com/share/{loom}\n"
    )
    row = row_for(f"  {text}  ", rooms, thread_name="NW · pages", image_count=1)
    assert set(row) == {
        "schema", "thread_id", "message_id", "text_sha256", "time", "shape_flags", "client", "asks",
        "loom_ids", "image_paths", "speaker_guess", "channel", "carrier", "round",
    }
    assert set(row["client"]) == {"status", "guess", "reason", "signals"}
    assert row["asks"] == []
    assert row["schema"] == "imprint.carrier/0"
    assert row["text_sha256"] == hashlib.sha256(text.strip().encode()).hexdigest()
    assert row["shape_flags"] == ["colon_lead", "rule_run", "timestamp_lines", "image_attachment"]
    assert row["loom_ids"] == [loom]
    assert row["carrier"] == ["paste", "image", "loom_link"]
    assert row["channel"] == "codex_prompt"
    assert row["round"] is None
    assert row["speaker_guess"] == {"basis": "lead_line", "person": NW_PERSON, "room": "northwind"}
    dumped = json.dumps(row)
    assert SECRET_WORD not in dumped and "sent this" not in dumped and "0:10" not in dumped


# ------------------------------------------------------------- the queue


def test_the_queue_must_be_a_private_absolute_file_outside_session_folders(tmp_path):
    cwd = tmp_path / "work"
    store = tmp_path / "store"
    good = tmp_path / "queues" / "carrier.jsonl"
    assert reader.checked_queue_path(str(good), cwd=str(cwd), forbidden=[str(store)]) == Path(os.path.realpath(good))
    refused = [
        "relative/carrier.jsonl", "", None, 7, "bad\x00name",
        str(cwd / "carrier.jsonl"), str(cwd / "deep" / "carrier.jsonl"),
        str(store / "queue.jsonl"),
        str(tmp_path / "Dropbox" / "carrier.jsonl"),
        str(tmp_path / "Library" / "Mobile Documents" / "carrier.jsonl"),
    ]
    for value in refused:
        assert reader.checked_queue_path(value, cwd=str(cwd), forbidden=[str(store)]) is None, value
    assert reader.checked_queue_path(str(good), cwd=None) is None


def test_a_linked_queue_is_refused(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    link = tmp_path / "queue.jsonl"
    link.symlink_to(target)
    assert reader.checked_queue_path(str(link), cwd=str(tmp_path / "work")) is None
    with pytest.raises(OSError):
        reader.append_row(link, {"a": 1})
    assert target.read_text() == ""


def test_a_queue_reached_through_a_linked_folder_is_checked_where_it_lands(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (tmp_path / "alias").symlink_to(cwd)
    assert reader.checked_queue_path(str(tmp_path / "alias" / "q.jsonl"), cwd=str(cwd)) is None


def test_rows_are_appended_one_line_each_to_a_private_file(tmp_path):
    queue = tmp_path / "new" / "carrier.jsonl"
    reader.append_row(queue, {"b": 1, "a": "x"})
    reader.append_row(queue, {"c": [1, 2]})
    assert stat.S_IMODE(queue.stat().st_mode) == 0o600
    assert stat.S_IMODE(queue.parent.stat().st_mode) & 0o077 == 0
    assert [json.loads(line) for line in queue.read_text().splitlines()] == [{"a": "x", "b": 1}, {"c": [1, 2]}]


def test_a_queue_that_is_not_a_regular_file_is_refused(tmp_path):
    (tmp_path / "dir.jsonl").mkdir()
    with pytest.raises(OSError):
        reader.append_row(tmp_path / "dir.jsonl", {"a": 1})


# ------------------------------------------------------------- the carrier step


@pytest.fixture
def setup(tmp_path):
    codex = tmp_path / "codex-home"
    codex.mkdir()
    make_state(codex / "state_5.sqlite", [
        PERSON_ROW,
        ("t-auto", "NW · nightly", "vscode", "automation", None, None, None, None),
        ("t-plain", "Copy pass", "vscode", "user", None, None, None, None),
    ])
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps(client_list(tmp_path)))
    queue = tmp_path / "queues" / "carrier.jsonl"
    config = {CLIENT_CARRIER_QUEUE: str(queue), CLIENT_CARRIER_CLIENTS: str(clients)}
    environ = {"CODEX_HOME": str(codex)}
    work = tmp_path / "work"
    work.mkdir()
    event = {
        "hook_event_name": "UserPromptSubmit", "session_id": "t-person", "turn_id": "turn-9",
        "cwd": str(work), "prompt": f"{NW_PERSON} wrote:\nthe header is too big",
        "transcript_path": str(tmp_path / "sessions" / "rollout.jsonl"),
    }
    return {"config": config, "environ": environ, "event": event, "queue": queue, "tmp": tmp_path, "codex": codex}


def step(setup, *, event=None, config=None, operator_root=None):
    return reader.codex_carrier_step(
        setup["event"] if event is None else event, setup["config"] if config is None else config,
        operator_root=operator_root, environ=setup["environ"], now="2026-01-02T03:04:05Z",
    )


def test_a_person_prompt_in_client_work_appends_one_row(setup):
    assert step(setup)
    (line,) = setup["queue"].read_text().splitlines()
    row = json.loads(line)
    assert row["thread_id"] == "t-person" and row["message_id"] == "turn-9"
    assert row["time"] == "2026-01-02T03:04:05Z"
    assert (row["client"]["status"], row["client"]["guess"]) == ("client", "northwind")
    assert "header" not in line


def test_the_step_does_nothing_while_the_queue_key_is_unset(setup, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("read while the step is off")

    monkeypatch.setattr(reader, "codex_thread_record", refuse)
    monkeypatch.setattr(reader, "load_rooms", refuse)
    assert not step(setup, config={CLIENT_CARRIER_CLIENTS: setup["config"][CLIENT_CARRIER_CLIENTS]})
    assert not step(setup, config=dict(DEFAULTS))
    assert not setup["queue"].parent.exists()


@pytest.mark.parametrize("change", [
    {"agent_id": "sub-1"}, {"agent_type": "explorer"}, {"parent_session_id": "t-parent"},
    {"session_id": "t-auto"}, {"session_id": "t-missing"},
    {"hook_event_name": "Stop"}, {"prompt": "<environment_context>x</environment_context>"},
    {"prompt": "   "}, {"prompt": None}, {"cwd": None}, {"session_id": 4},
    {"session_id": "t-plain", "prompt": "Please tighten the second paragraph"},
])
def test_no_row_for_anything_but_a_person_prompt_in_client_work(setup, change):
    assert not step(setup, event={**setup["event"], **change})
    assert not setup["queue"].exists()


def test_no_row_without_a_readable_client_list(setup):
    assert not step(setup, config={**setup["config"], CLIENT_CARRIER_CLIENTS: None})
    assert not step(setup, config={**setup["config"], CLIENT_CARRIER_CLIENTS: str(setup["tmp"] / "absent.json")})
    assert not setup["queue"].exists()


@pytest.mark.parametrize("where", ["cwd", "operator_root", "codex_home", "transcripts"])
def test_the_queue_is_refused_inside_session_and_store_folders(setup, where):
    tmp = setup["tmp"]
    folder = {
        "cwd": Path(setup["event"]["cwd"]), "operator_root": tmp / "data" / "operator",
        "codex_home": setup["codex"], "transcripts": tmp / "sessions",
    }[where]
    config = {**setup["config"], CLIENT_CARRIER_QUEUE: str(folder / "carrier.jsonl")}
    assert not step(setup, config=config, operator_root=tmp / "data" / "operator")
    assert not (folder / "carrier.jsonl").exists()


def test_a_failing_append_is_swallowed(setup):
    setup["queue"].mkdir(parents=True)
    assert not step(setup)


def test_a_non_mapping_event_or_config_is_swallowed(setup):
    assert not step(setup, event=["not", "an", "event"])
    assert not reader.codex_carrier_step(setup["event"], None)


# ------------------------------------------------------------- config


def test_both_keys_default_to_unset():
    assert DEFAULTS[CLIENT_CARRIER_QUEUE] is None and DEFAULTS[CLIENT_CARRIER_CLIENTS] is None
    assert CLIENT_CARRIER_QUEUE == "org.imprint.client_carrier.queue"
    assert CLIENT_CARRIER_CLIENTS == "org.imprint.client_carrier.clients"
    parsed = parse_config({})
    assert parsed[CLIENT_CARRIER_QUEUE] is None and parsed[CLIENT_CARRIER_CLIENTS] is None


@pytest.mark.parametrize("key", [CLIENT_CARRIER_QUEUE, CLIENT_CARRIER_CLIENTS])
def test_the_keys_take_an_absolute_path_or_null(tmp_path, key):
    assert parse_config({key: str(tmp_path / "x.json")})[key] == str(tmp_path / "x.json")
    assert parse_config({key: "~/x.json"})[key] == "~/x.json"
    assert parse_config({key: None})[key] is None
    for bad in ("relative.json", "", "   ", 5, True, ["x"], "/bad\x00path"):
        with pytest.raises(ValidationError):
            parse_config({key: bad})
