"""No Jev in a chat T3 places in its scratch folder, and no change anywhere else.

Every request goes to an in-process fake transport, and every T3 store is synthetic.
"""
import copy
import io
import json
import re
import sqlite3
from pathlib import Path

import pytest

from imprint import cli
from imprint.capture import t3_origin
from imprint.capture.t3_origin import T3_THREAD_ENV, _in_scratch, _scratch_placement
from imprint.config import DEFAULTS
from imprint.retrieve import jev
from imprint.retrieve.jev import QUESTION_PREFIX
from imprint.store import ImprintStore

KEY_ENV = "JEV_TEST_KEY"
THREAD = "thread-synthetic"
SESSION = "session-synthetic"
CLAUDE_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
PROMPT = "Should the failed source be reported?"


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, endpoint, body, headers, timeout):
        request = json.loads(body)
        self.calls.append(request)
        return json.dumps({
            "model": "jev-test-echo",
            "answers": {name: {"type": "noul", "noul": 0.9} for name in request["questions"]},
            "usage": {"input_tokens": 40, "output_tokens": 4},
        }).encode("utf-8")


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()
    monkeypatch.setattr(jev, "http_post", fake)
    return fake


@pytest.fixture(autouse=True)
def selector_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("IMPRINT_SELECTOR", raising=False)
    monkeypatch.setenv(KEY_ENV, "synthetic-test-key")


def _selector(mode):
    value = copy.deepcopy(DEFAULTS["selector"])
    value["mode"] = mode
    value["jev"]["api_key_env"] = KEY_ENV
    return value


def _config(tmp_path, *, mode="jev", origin=True):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "config_version": "3.0.0", "data_root": str(tmp_path / "data"),
        "operator_slug": "test", "node_id": "primary", "compiler": True,
        "t3_capture_origin": origin, "selector": _selector(mode),
    }))
    return path


def _main(*argv, event=None, monkeypatch, capsys):
    if event is not None:
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    code = cli.main(list(argv))
    body = json.loads(capsys.readouterr().out)
    assert code == 0, body
    return body


def _seed(config, monkeypatch, capsys):
    """Store judgments before any T3 variable is set, so capture stays out of it."""
    for index, text in enumerate((
        "No, report the failed source because omissions change the decision.",
        "No, never rename a public flag without a migration note because users lose settings.",
    )):
        receipt = _main(
            "--config", str(config), "hook", "stop-capture",
            event={"session_id": f"seed-{index}", "operator_text": text, "case_description": "Synthetic review"},
            monkeypatch=monkeypatch, capsys=capsys,
        )
        assert receipt["canonical_status"] == "compiled", receipt
    assert ImprintStore(Path(json.loads(config.read_text())["data_root"]) / "test" / "imprint.db").current_nodes(["Verdict"])


def _t3_store(tmp_path, *, thread_payload="{}", project_root="/synthetic/project", thread_row=True,
              sessions=(SESSION,), provider_payloads=None, messages=True):
    t3 = tmp_path / "t3"
    userdata = t3 / "userdata"
    userdata.mkdir(parents=True, exist_ok=True)
    (t3 / "scratch").mkdir(exist_ok=True)
    database = userdata / "statev2.sqlite"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE orchestration_v2_projection_threads "
            "(thread_id TEXT PRIMARY KEY, payload_json TEXT, project_id TEXT)",
        )
        db.execute("CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, workspace_root TEXT)")
        db.execute(
            "CREATE TABLE orchestration_v2_projection_provider_threads "
            "(provider_thread_id INTEGER PRIMARY KEY, thread_id TEXT, payload_json TEXT)",
        )
        if messages:
            db.execute(
                "CREATE TABLE orchestration_v2_projection_messages "
                "(message_id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, created_at TEXT, payload_json TEXT)",
            )
        db.execute("INSERT INTO projection_projects VALUES ('project-synthetic', ?)", (project_root,))
        if thread_row:
            db.execute(
                "INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, 'project-synthetic')",
                (THREAD, thread_payload),
            )
        payloads = provider_payloads
        if payloads is None:
            payloads = [json.dumps({"nativeThreadRef": {"nativeId": session}}) for session in sessions]
        db.executemany(
            "INSERT INTO orchestration_v2_projection_provider_threads (thread_id, payload_json) VALUES (?, ?)",
            [(THREAD, payload) for payload in payloads],
        )
    return t3


def _worktree(folder):
    return json.dumps({"worktreePath": str(folder)})


def _in_t3(monkeypatch, t3, *, thread=THREAD):
    monkeypatch.setenv("T3CODE_HOME", str(t3))
    monkeypatch.setenv(T3_THREAD_ENV, thread)


def _prompt_hook(config, monkeypatch, capsys, *, session=SESSION, prompt=PROMPT):
    return _main(
        "--config", str(config), "hook", "user-prompt-submit",
        event={"session_id": session, "prompt": prompt}, monkeypatch=monkeypatch, capsys=capsys,
    )


def _retrieve(config, monkeypatch, capsys, *, session=SESSION, prompt=PROMPT):
    return _main("--config", str(config), "retrieve", "--session", session, "--prompt", prompt,
                 monkeypatch=monkeypatch, capsys=capsys)


ROUTES = pytest.mark.parametrize("route", [_prompt_hook, _retrieve], ids=["prompt hook", "retrieve"])


@ROUTES
@pytest.mark.parametrize("mode", ["jev", "shadow"])
def test_a_scratch_thread_sends_zero_requests(tmp_path, transport, monkeypatch, capsys, route, mode):
    config = _config(tmp_path, mode=mode)
    _seed(config, monkeypatch, capsys)
    t3 = _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch"))
    _in_t3(monkeypatch, t3)
    body = route(config, monkeypatch, capsys)
    assert transport.calls == []
    assert body["selector_method"] == "deterministic"
    assert body["selector_outcome"] == "t3_scratch_thread"


@ROUTES
@pytest.mark.parametrize("place", ["project root", "worktree", "symlink", "nested folder"])
def test_either_folder_places_a_thread_however_it_is_reached(tmp_path, transport, monkeypatch, capsys, route, place):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    scratch = tmp_path / "t3" / "scratch"
    link = tmp_path / "alias"
    if place == "symlink":
        scratch.mkdir(parents=True)
        link.symlink_to(scratch)
    keyword = {
        "project root": {"project_root": str(scratch)},
        "worktree": {"thread_payload": _worktree(scratch)},
        "symlink": {"project_root": str(link)},
        "nested folder": {"thread_payload": _worktree(scratch / "a" / "b")},
    }[place]
    _in_t3(monkeypatch, _t3_store(tmp_path, **keyword))
    assert route(config, monkeypatch, capsys)["selector_outcome"] == "t3_scratch_thread"
    assert transport.calls == []


@ROUTES
def test_a_thread_in_a_project_still_calls_jev(tmp_path, transport, monkeypatch, capsys, route):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root="/synthetic/project"))
    body = route(config, monkeypatch, capsys)
    assert len(transport.calls) == 1
    assert body["selector_method"] == "jev"
    assert body["selector_outcome"] != "t3_scratch_thread"


@ROUTES
def test_a_folder_beside_scratch_with_a_like_name_is_not_scratch(tmp_path, transport, monkeypatch, capsys, route):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch-notes")))
    route(config, monkeypatch, capsys)
    assert len(transport.calls) == 1


def _no_store(tmp_path, scratch):
    return tmp_path / "t3"


def _no_tables(tmp_path, scratch):
    (tmp_path / "t3" / "userdata").mkdir(parents=True)
    sqlite3.connect(tmp_path / "t3" / "userdata" / "statev2.sqlite").close()
    return tmp_path / "t3"


def _no_thread_row(tmp_path, scratch):
    return _t3_store(tmp_path, thread_row=False, project_root=str(scratch))


def _no_provider_row(tmp_path, scratch):
    return _t3_store(tmp_path, sessions=(), project_root=str(scratch))


def _thread_not_json(tmp_path, scratch):
    return _t3_store(tmp_path, thread_payload="{", project_root=str(scratch))


def _thread_not_an_object(tmp_path, scratch):
    return _t3_store(tmp_path, thread_payload="[]", project_root=str(scratch))


def _provider_not_json(tmp_path, scratch):
    return _t3_store(tmp_path, provider_payloads=["{"], project_root=str(scratch))


def _one_provider_not_json(tmp_path, scratch):
    matching = json.dumps({"nativeThreadRef": {"nativeId": SESSION}})
    return _t3_store(tmp_path, provider_payloads=[matching, "{"], project_root=str(scratch))


def _relative_folder(tmp_path, scratch):
    return _t3_store(tmp_path, thread_payload='{"worktreePath": "scratch/relative"}', project_root="/synthetic/project")


def _folder_of_another_type(tmp_path, scratch):
    return _t3_store(tmp_path, thread_payload='{"worktreePath": 7}', project_root="/synthetic/project")


def _no_folder(tmp_path, scratch):
    return _t3_store(tmp_path, project_root=None)


@ROUTES
@pytest.mark.parametrize("build", [
    _no_store, _no_tables, _no_thread_row, _no_provider_row, _thread_not_json, _thread_not_an_object,
    _provider_not_json, _one_provider_not_json, _relative_folder, _folder_of_another_type, _no_folder,
])
def test_a_thread_t3_cannot_place_keeps_jev(tmp_path, transport, monkeypatch, capsys, route, build):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, build(tmp_path, tmp_path / "t3" / "scratch"))
    body = route(config, monkeypatch, capsys)
    assert len(transport.calls) == 1
    assert body["selector_outcome"] != "t3_scratch_thread"


@ROUTES
def test_a_locked_store_keeps_jev(tmp_path, transport, monkeypatch, capsys, route):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    t3 = _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch"))
    _in_t3(monkeypatch, t3)
    holder = sqlite3.connect(t3 / "userdata" / "statev2.sqlite", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        route(config, monkeypatch, capsys)
    finally:
        holder.close()
    assert len(transport.calls) == 1


def test_a_nested_run_in_a_scratch_thread_keeps_jev_on_the_hook(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    _prompt_hook(config, monkeypatch, capsys, session="session-of-a-nested-run")
    assert len(transport.calls) == 1


def test_a_nested_run_in_a_scratch_thread_keeps_jev_on_retrieve(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    monkeypatch.setenv(CLAUDE_SESSION_ENV, "session-of-a-nested-run")
    _retrieve(config, monkeypatch, capsys, session=SESSION)
    assert len(transport.calls) == 1


def test_retrieve_with_a_label_t3_does_not_know_sends_the_prompt(tmp_path, transport, monkeypatch, capsys):
    """Run by hand or by a script, there is no session to tie to the scratch thread."""
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    _retrieve(config, monkeypatch, capsys, session="my-script-label")
    assert [request["state"] for request in transport.calls] == [PROMPT]


def _keeps_jev_in_scratch(tmp_path, monkeypatch):
    """A scratch thread with a provider row for some other session, as a nested run sees."""
    t3 = _t3_store(tmp_path, sessions=("session-of-another-run",), project_root=str(tmp_path / "t3" / "scratch"))
    _in_t3(monkeypatch, t3)


def test_a_turn_that_keeps_jev_sends_the_whole_request(tmp_path, transport, monkeypatch, capsys):
    """Not only the words typed: the previous reply and the judgments leave with them."""
    config = _config(tmp_path)
    settings = json.loads(config.read_text())
    settings["selector"]["jev"]["short_prompt_context"] = {"enabled": True, "below_chars": 200, "max_chars": 120}
    config.write_text(json.dumps(settings))
    _seed(config, monkeypatch, capsys)
    reply = "Synthetic reply naming a synthetic file."
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(line) for line in (
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}},
        {"type": "user", "message": {"role": "user", "content": "yes"}},
    )) + "\n")
    _keeps_jev_in_scratch(tmp_path, monkeypatch)
    _main(
        "--config", str(config), "hook", "user-prompt-submit",
        event={"session_id": SESSION, "prompt": "yes", "transcript_path": str(transcript)},
        monkeypatch=monkeypatch, capsys=capsys,
    )
    (request,) = transport.calls
    assert request["state"] == f"yes\n\nPrevious assistant reply:\n{reply}"
    assert "report the failed source" in json.dumps(request["questions"])


@pytest.mark.parametrize("mode, switch, sent", [
    ("jev", None, 1), ("jev", "off", 0), ("deterministic", None, 0),
], ids=["no switch", "environment", "config"])
def test_the_off_switch_and_the_config_keep_local_what_the_check_lets_through(
    tmp_path, transport, monkeypatch, capsys, mode, switch, sent,
):
    """The variable is read from the process that sends, so it must be in that process."""
    config = _config(tmp_path, mode=mode)
    _seed(config, monkeypatch, capsys)
    _keeps_jev_in_scratch(tmp_path, monkeypatch)
    if switch is not None:
        monkeypatch.setenv("IMPRINT_SELECTOR", switch)
    _prompt_hook(config, monkeypatch, capsys)
    assert len(transport.calls) == sent


REPOSITORY = Path(__file__).resolve().parents[2]
# Where each page speaks of the scratch check, as the words around each passage. A
# passage that goes away fails here. The first configuration passage opens on its
# mode because with the default selector nothing is sent and T3 is not read.
SCRATCH_PASSAGES = {
    "docs/privacy-and-recovery.md": [("With `t3_capture_origin: true`, neither sends", "Exactly this leaves the machine")],
    "docs/configuration.md": [
        ("In `jev` or `shadow` mode, the prompt hook", "Set this key after the build"),
        ("a thread T3's records place in its scratch folder is the exception", "`shadow`: calls Jev"),
    ],
    "docs/contracts/capture-origin.md": [("## Scratch chats and Jev", "T3 is read only on a turn")],
    "CHANGELOG.md": [("Send Jev nothing from T3's scratch chats", "Report the Claude homes")],
}
FALSE_PROMISE = re.compile(
    r"nothing (ever )?leaves|never sends|scratch chats get no Jev|no need"
    r"|(never|not|no longer) (be |been )?(required|needed|necessary)",
    re.IGNORECASE,
)


def _scratch_passages(name):
    page = " ".join((REPOSITORY / name).read_text(encoding="utf-8").split())
    passages = []
    for opening, closing in SCRATCH_PASSAGES[name]:
        start = page.index(opening)
        passages.append(page[start:page.index(closing, start)])
    return passages


@pytest.mark.parametrize("name", SCRATCH_PASSAGES)
def test_the_docs_do_not_promise_a_scratch_thread_stays_local(name):
    """The check fails open, so each page says what gets through and how to stop it."""
    passages = _scratch_passages(name)
    for passage in passages:
        assert "does not keep prompts local" in passage
        assert "`IMPRINT_SELECTOR=off`" in passage
        assert "`selector.mode: deterministic`" in passage
        assert FALSE_PROMISE.search(passage) is None
    named = " ".join(passages)
    assert "nested run" in named
    assert "cannot be read" in named
    assert "`retrieve` run" in named or "`CLAUDE_CODE_SESSION_ID`" in named


@pytest.mark.parametrize("name", [name for name in SCRATCH_PASSAGES if name != "CHANGELOG.md"])
def test_the_docs_say_what_a_kept_turn_sends_and_where_the_switch_must_be(name):
    passages = _scratch_passages(name)
    for passage in passages:
        assert "the same request as any other turn" in passage
        assert "where the hook will see it" in passage
    named = " ".join(passages)
    assert "the prompt, any previous-reply excerpt and the judgment statements" in named
    assert "shell export reaches only what that shell runs" in " ".join(
        (REPOSITORY / name).read_text(encoding="utf-8").split(),
    )


def test_retrieve_finds_a_scratch_thread_whatever_label_it_is_given(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    monkeypatch.setenv(CLAUDE_SESSION_ENV, SESSION)
    body = _retrieve(config, monkeypatch, capsys, session="any-label-an-agent-chose")
    assert transport.calls == []
    assert body["selector_outcome"] == "t3_scratch_thread"


def test_placing_a_thread_reads_no_prompt(tmp_path, transport, monkeypatch, capsys):
    """The prompts table is gone, so a read of it would leave the thread unplaced."""
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch"), messages=False))
    assert _prompt_hook(config, monkeypatch, capsys)["selector_outcome"] == "t3_scratch_thread"


@pytest.fixture
def t3_reads(monkeypatch):
    """Every read of a T3 store. Building a synthetic one here is not a read."""
    reads = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        if "statev2.sqlite?mode=ro" in str(database):
            reads.append(database)
        return real(database, *args, **kwargs)

    monkeypatch.setattr(t3_origin.sqlite3, "connect", spy)
    return reads


@ROUTES
def test_with_the_setting_off_t3_is_not_read_and_nothing_changes(tmp_path, transport, monkeypatch, capsys, t3_reads, route):
    config = _config(tmp_path, origin=False)
    _seed(config, monkeypatch, capsys)
    baseline = route(config, monkeypatch, capsys, session="session-baseline")
    sent = copy.deepcopy(transport.calls)

    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    monkeypatch.setenv(CLAUDE_SESSION_ENV, SESSION)
    scratch = route(config, monkeypatch, capsys, session=SESSION)

    assert t3_reads == []
    assert transport.calls == sent + sent
    fields = ("status", "selector_method", "selector_outcome", "fallback_reason")
    assert [scratch.get(field) for field in fields] == [baseline.get(field) for field in fields]
    assert "t3_scratch_thread" not in json.dumps(scratch)


def test_the_setting_must_be_true_not_merely_truthy(tmp_path, monkeypatch, t3_reads):
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    assert cli._t3_scratch_thread({"t3_capture_origin": 1}, SESSION) is False
    assert t3_reads == []
    assert cli._t3_scratch_thread({"t3_capture_origin": True}, SESSION) is True
    assert len(t3_reads) == 1


@ROUTES
def test_deterministic_mode_does_not_read_t3_at_all(tmp_path, transport, monkeypatch, capsys, t3_reads, route):
    config = _config(tmp_path, mode="deterministic")
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    body = route(config, monkeypatch, capsys)
    assert t3_reads == []
    assert transport.calls == []
    assert "selector_outcome" not in body


@ROUTES
def test_an_empty_prompt_does_not_read_t3(tmp_path, transport, monkeypatch, capsys, t3_reads, route):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    route(config, monkeypatch, capsys, prompt="   ")
    assert t3_reads == []
    assert transport.calls == []


@ROUTES
def test_a_run_outside_t3_does_not_read_t3(tmp_path, transport, monkeypatch, capsys, t3_reads, route):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    monkeypatch.setenv("T3CODE_HOME", str(_t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch"))))
    route(config, monkeypatch, capsys)
    assert t3_reads == []
    assert len(transport.calls) == 1


def test_session_start_in_a_scratch_thread_is_unchanged(tmp_path, transport, monkeypatch, capsys, t3_reads):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    outside = _main("--config", str(config), "hook", "session-start", event={"session_id": "outside"},
                    monkeypatch=monkeypatch, capsys=capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    inside = _main("--config", str(config), "hook", "session-start", event={"session_id": SESSION},
                   monkeypatch=monkeypatch, capsys=capsys)
    assert inside["status"] == outside["status"] == "delivered"
    assert inside["hookSpecificOutput"] == outside["hookSpecificOutput"]
    assert t3_reads == []
    assert transport.calls == []


def test_a_scratch_turn_gets_the_deterministic_answer_for_its_domain(tmp_path, transport, monkeypatch, capsys):
    config = _config(tmp_path)
    value = json.loads(config.read_text())
    value["domains"] = [{"domain_id": "research", "public_label": "Research", "keywords": ["sources"], "frozen": False}]
    config.write_text(json.dumps(value))
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))
    body = _prompt_hook(config, monkeypatch, capsys, prompt="Which sources failed?")
    assert transport.calls == []
    assert body["domain_id"] == "research"
    assert body["selection_method"] == "keyword"
    assert body["selector_method"] == "deterministic"


DATABASE = Path("/synthetic/t3/userdata/statev2.sqlite")


@pytest.mark.parametrize("folders, placed, refused", [
    (None, None, True),
    ((), None, True),
    (("relative/folder",), None, True),
    ((7,), None, True),
    (("/synthetic/with\u0000null",), None, True),
    (("/synthetic/project",), False, False),
    (("/synthetic/t3/scratch",), True, True),
    (("/synthetic/t3/scratch/chat",), True, True),
    (("/synthetic/t3/scratch-notes",), False, False),
    (("/synthetic/project", "/synthetic/t3/scratch"), True, True),
    (("relative/folder", "/synthetic/t3/scratch"), True, True),
    (("/synthetic/t3/scratch", 7), True, True),
    (("/synthetic/project", "relative/folder"), None, True),
    (("/synthetic/project", 7), None, True),
], ids=[
    "no row", "no folder", "relative", "other type", "null byte", "project", "scratch", "under scratch",
    "similar name", "either folder", "relative then scratch", "scratch then other type",
    "project then relative", "project then other type",
])
def test_capture_and_selection_read_one_placement_their_own_ways(folders, placed, refused):
    """Capture credits nothing it cannot place; selection keeps Jev for the same thread."""
    assert _scratch_placement(folders, DATABASE) is placed
    assert _in_scratch(folders, DATABASE) is refused


def test_a_store_path_that_cannot_resolve_places_nothing(monkeypatch):
    real = Path.resolve

    def looping(self, *args, **kwargs):
        if self.name == "scratch":
            raise RuntimeError("synthetic symlink loop")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", looping)
    assert _scratch_placement(("/synthetic/project",), DATABASE) is None
    assert _in_scratch(("/synthetic/project",), DATABASE) is True


@ROUTES
def test_a_check_that_fails_outright_keeps_jev(tmp_path, transport, monkeypatch, capsys, route):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root=str(tmp_path / "t3" / "scratch")))

    def broken(self):
        raise KeyError("synthetic")

    monkeypatch.setattr(t3_origin.T3HostOrigin, "in_scratch", broken)
    assert route(config, monkeypatch, capsys)["selector_outcome"] != "t3_scratch_thread"
    assert len(transport.calls) == 1


def test_no_store_home_places_nothing():
    origin = t3_origin.T3HostOrigin(THREAD, SESSION, None)
    assert origin.in_scratch() is False


@ROUTES
def test_a_thread_in_a_project_sends_what_a_run_outside_t3_sends(tmp_path, transport, monkeypatch, capsys, route):
    config = _config(tmp_path)
    _seed(config, monkeypatch, capsys)
    outside = route(config, monkeypatch, capsys, session="session-outside")
    _in_t3(monkeypatch, _t3_store(tmp_path, project_root="/synthetic/project"))
    inside = route(config, monkeypatch, capsys)
    (first, second) = transport.calls
    assert first == second
    assert first["state"] == PROMPT
    assert all(item["instructions"].startswith(QUESTION_PREFIX) for item in first["questions"].values())
    fields = ("status", "selector_method", "selector_outcome", "fallback_reason")
    assert [inside.get(field) for field in fields] == [outside.get(field) for field in fields]
