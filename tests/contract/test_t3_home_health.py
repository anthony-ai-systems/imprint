"""Health names the Claude homes T3 launches with no hooks or another store."""
import importlib.util
import json
import os
import threading
from pathlib import Path

import pytest

from imprint.capture import t3_origin
from imprint.config import load_config, resolved_operator_root
from imprint.errors import ValidationError
from imprint.health import health_report, t3_homes
from imprint.store import ImprintStore

MANAGER_PATH = Path(__file__).resolve().parents[2] / "tools" / "install" / "manage_hooks.py"
SPEC = importlib.util.spec_from_file_location("imprint_manage_hooks_for_health", MANAGER_PATH)
assert SPEC and SPEC.loader
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)

MARKER = "synthetic-private-home"
ON = {"t3_capture_origin": True}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A T3 home, a user home and this store, none of them real.

    This store is the one a hook reaches with no override, as in a default install.
    """
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("T3CODE_HOME", str(tmp_path / "t3"))
    for name in ("IMPRINT_CONFIG", "IMPRINT_DATA_ROOT", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _hooked(home, **extra):
    """Register hooks with the real manager, so this cannot drift from it."""
    settings = manager.mutate(extra, "register", Path("/synthetic/python"), Path("/synthetic/hooks"))
    _write_settings(home, settings)


def _write_settings(home, settings):
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _instance(home=None, *, driver="claudeAgent", **extra):
    config = extra.pop("config", {})
    if home is not None:
        config = {**config, "homePath": str(home)}
    return {"driver": driver, "config": config, **extra}


def _t3_settings(world, **instances):
    userdata = world / "t3" / "userdata"
    userdata.mkdir(parents=True, exist_ok=True)
    (userdata / "settings.json").write_text(json.dumps({"providerInstances": instances}), encoding="utf-8")


def _report(world, config=ON):
    """Run the whole health report and return it with this store's T3 section."""
    root = resolved_operator_root(load_config())
    root.mkdir(parents=True, exist_ok=True)
    store = ImprintStore(root / "imprint.db")
    store.initialize()
    hooks = world / "installed-hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("session_start.py", "user_prompt_submit.py", "stop_capture.py", "health_check.py"):
        (hooks / name).write_text("# test\n")
    return health_report(root, store, {
        "compiler": True, "context_budget_bytes": 32768, "hooks_dir": str(hooks),
        "experimental": {"digest": False, "profile_learning": False}, **config,
    })


def _homes(world):
    return _report(world)["metrics"]["t3_homes"]


def _by_instance(section):
    return {name: row for row in section["homes"] for name in row["instances"]}


def _other_store(world, *, slug="other", data="other-data", name="other.json"):
    path = world / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data_root": str(world / data), "operator_slug": slug}), encoding="utf-8")
    return path


def _this_store(world):
    """Write the config a hook reads by default, naming the store health opens."""
    return _other_store(world, slug="default", data="this-data", name="home/.config/imprint/config.json")


def _without_blocking(call):
    """Run a call that must not block, so a regression fails and does not hang the run."""
    results = []
    worker = threading.Thread(target=lambda: results.append(call()), daemon=True)
    worker.start()
    worker.join(10)
    assert results, "the call blocked"
    return results[0]


def test_the_marker_and_events_match_the_hook_manager():
    assert t3_homes.HOOK_MARKER == manager.MARKER
    assert t3_homes.HOOK_ENTRIES == {event: len(sources) for event, sources in manager.EVENTS.items()}


def test_a_home_with_all_hooks_and_this_store_is_not_flagged(world):
    home = world / MARKER / "good"
    _hooked(home)
    _t3_settings(world, claudeAgent=_instance(home))
    section = _homes(world)
    assert section["state"] == "read"
    assert section["homes"] == [{
        "instances": ["claudeAgent"], "enabled": True, "hooks": "complete", "data_root": "same",
    }]
    assert section["homes_missing_hooks"] == 0
    assert section["homes_different_data_root"] == 0
    assert section["homes_unreadable"] == 0


def test_mixed_homes_are_each_flagged_for_what_is_wrong_with_them(world):
    good, bare, absent, stop_only, split = (world / MARKER / name for name in ("a", "b", "c", "d", "e"))
    _hooked(good)
    _write_settings(bare, {"theme": "dark"})
    settings = manager.mutate({}, "register", Path("/synthetic/python"), Path("/synthetic/hooks"))
    _write_settings(stop_only, {"hooks": {"Stop": settings["hooks"]["Stop"]}})
    _hooked(split, env={"IMPRINT_CONFIG": str(_other_store(world))})
    _t3_settings(
        world, claudeAgent=_instance(good), claudeAgent_bare=_instance(bare),
        claudeAgent_absent=_instance(absent), claudeAgent_stop=_instance(stop_only),
        claudeAgent_split=_instance(split),
    )
    section = _homes(world)
    rows = _by_instance(section)
    assert {name: row["hooks"] for name, row in rows.items()} == {
        "claudeAgent": "complete", "claudeAgent_bare": "none", "claudeAgent_absent": "none",
        "claudeAgent_stop": "partial", "claudeAgent_split": "complete",
    }
    assert {name: row["data_root"] for name, row in rows.items()} == {
        "claudeAgent": "same", "claudeAgent_bare": "same", "claudeAgent_absent": "same",
        "claudeAgent_stop": "same", "claudeAgent_split": "different",
    }
    assert section["homes_missing_hooks"] == 3
    assert section["homes_different_data_root"] == 1


def test_the_report_holds_no_path_and_no_setting(world):
    home = world / MARKER / "home"
    _hooked(home, env={"IMPRINT_DATA_ROOT": f"/{MARKER}/store", "OTHER": "synthetic-value"})
    _t3_settings(world, claudeAgent=_instance(home, displayName="Synthetic Person"))
    encoded = json.dumps(_report(world))
    assert MARKER not in encoded
    assert "synthetic-value" not in encoded
    assert "Synthetic Person" not in encoded


def test_flagged_homes_are_reported_without_changing_the_status(world):
    baseline = _report(world, {})
    bare, split = world / MARKER / "bare", world / MARKER / "split"
    _write_settings(bare, {})
    _hooked(split, env={"IMPRINT_CONFIG": str(_other_store(world))})
    _t3_settings(world, claudeAgent=_instance(bare), claudeAgent_b=_instance(split))
    report = _report(world)
    assert report["metrics"]["t3_homes"]["homes_missing_hooks"] == 1
    assert report["metrics"]["t3_homes"]["homes_different_data_root"] == 1
    assert report["status"] == baseline["status"]
    assert report["degraded_reasons"] == baseline["degraded_reasons"]


@pytest.mark.parametrize("variable", ["home", "t3_instance"])
def test_another_store_is_found_in_either_place_a_home_can_set_it(world, variable):
    home = world / MARKER / "home"
    other = _other_store(world)
    _hooked(home, **({"env": {"IMPRINT_CONFIG": str(other)}} if variable == "home" else {}))
    environment = [{"name": "IMPRINT_CONFIG", "value": str(other), "sensitive": False}]
    _t3_settings(world, claudeAgent=_instance(home, **({"environment": environment} if variable != "home" else {})))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "different"


def test_another_operator_in_the_same_data_root_is_another_store(world):
    home = world / MARKER / "home"
    root = resolved_operator_root(load_config())
    other = _other_store(world, slug="another", data=str(root.parent))
    _hooked(home, env={"IMPRINT_CONFIG": str(other)})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "different"


def test_a_home_env_wins_over_the_instance_environment(world):
    home = world / MARKER / "home"
    same = resolved_operator_root(load_config())
    config = world / "same.json"
    config.write_text(json.dumps({"data_root": str(same.parent), "operator_slug": same.name}), encoding="utf-8")
    _hooked(home, env={"IMPRINT_CONFIG": str(config)})
    environment = [{"name": "IMPRINT_CONFIG", "value": str(_other_store(world))}]
    _t3_settings(world, claudeAgent=_instance(home, environment=environment))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"


def test_an_override_that_opens_this_store_is_not_flagged(world):
    home = world / MARKER / "home"
    root = resolved_operator_root(load_config())
    config = world / "same.json"
    config.write_text(json.dumps({"data_root": str(root.parent), "operator_slug": root.name}), encoding="utf-8")
    _hooked(home, env={"IMPRINT_CONFIG": str(config)})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"


def test_a_data_root_variable_a_config_overrides_is_not_flagged(world):
    _this_store(world)
    home = world / MARKER / "home"
    _hooked(home, env={"IMPRINT_DATA_ROOT": str(world / "ignored")})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"


@pytest.mark.parametrize("launcher", [True, False], ids=["launcher", "no launcher"])
def test_the_launcher_export_does_not_hide_what_a_home_sets(world, monkeypatch, launcher):
    """`imprint` runs with IMPRINT_CONFIG set and a hook does not, so one home reads the same either way."""
    config = _this_store(world)
    if launcher:
        monkeypatch.setenv("IMPRINT_CONFIG", str(config))
    other = _other_store(world, name="xdg/imprint/config.json")
    home = world / MARKER / "home"
    _hooked(home, env={"XDG_CONFIG_HOME": str(other.parents[1])})
    _t3_settings(world, claudeAgent=_instance(home))
    section = _homes(world)
    assert _by_instance(section)["claudeAgent"]["data_root"] == "different"
    assert section["homes_different_data_root"] == 1


def test_a_default_install_reads_the_same_from_the_launcher(world, monkeypatch):
    monkeypatch.setenv("IMPRINT_CONFIG", str(_this_store(world)))
    home = world / MARKER / "home"
    _hooked(home)
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"


def test_a_home_that_sets_nothing_is_still_compared(world, monkeypatch):
    """After `install.sh --config --data-root` the launcher names a store no hook reaches."""
    monkeypatch.setenv("IMPRINT_CONFIG", str(_other_store(world, data="custom-data", name="custom.json")))
    home = world / MARKER / "home"
    _hooked(home)
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "different"


def test_the_launcher_and_a_hook_compare_one_home_with_different_stores(world, monkeypatch):
    """The home resolves alike for both, and each compares it with the store it opened."""
    home = world / MARKER / "home"
    _hooked(home)
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"
    monkeypatch.setenv("IMPRINT_CONFIG", str(_other_store(world, data="custom-data", name="custom.json")))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "different"


def test_install_config_alone_keeps_the_default_store(world, monkeypatch):
    """The installer writes its default data root and operator into a `--config` file."""
    default_root = world / "home" / ".local" / "share" / "imprint"
    custom = world / "custom.json"
    custom.write_text(json.dumps({"data_root": str(default_root), "operator_slug": "default"}), encoding="utf-8")
    monkeypatch.setenv("IMPRINT_CONFIG", str(custom))
    home = world / MARKER / "home"
    _hooked(home)
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"


def test_a_data_root_only_this_process_has_reaches_no_hook(world, monkeypatch):
    monkeypatch.setenv("IMPRINT_DATA_ROOT", str(world / "exported-data"))
    home = world / MARKER / "home"
    _hooked(home)
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "different"


def test_health_run_inside_one_home_compares_the_others_with_that_store(world, monkeypatch):
    """The session-start health check inherits its home's env, and the next home does not."""
    config = _other_store(world, data="split-data", name="split.json")
    monkeypatch.setenv("IMPRINT_CONFIG", str(config))
    inside, elsewhere = world / MARKER / "inside", world / MARKER / "elsewhere"
    _hooked(inside, env={"IMPRINT_CONFIG": str(config)})
    _hooked(elsewhere)
    _t3_settings(world, claudeAgent=_instance(inside), claudeAgent_b=_instance(elsewhere))
    rows = _by_instance(_homes(world))
    assert rows["claudeAgent"]["data_root"] == "same"
    assert rows["claudeAgent_b"]["data_root"] == "different"


def _moved_home(world, *, with_config):
    """A second user home, with its own config or with only the default data root."""
    alternate = world / "alternate"
    alternate.mkdir()
    if with_config:
        _other_store(world, name="alternate/.config/imprint/config.json")
    return alternate


@pytest.mark.parametrize("place", ["home", "t3_instance"])
@pytest.mark.parametrize("with_config", [True, False], ids=["config", "default data root"])
def test_a_home_that_moves_home_moves_its_store(world, place, with_config):
    home = world / MARKER / "home"
    alternate = str(_moved_home(world, with_config=with_config))
    _hooked(home, **({"env": {"HOME": alternate}} if place == "home" else {}))
    environment = [{"name": "HOME", "value": alternate}] if place == "t3_instance" else []
    _t3_settings(world, claudeAgent=_instance(home, environment=environment))
    section = _homes(world)
    assert _by_instance(section)["claudeAgent"]["data_root"] == "different"
    assert section["homes_different_data_root"] == 1


@pytest.mark.parametrize("variable, value", [
    ("IMPRINT_CONFIG", "~/.config/imprint/custom.json"),
    ("IMPRINT_DATA_ROOT", "~/data"),
])
def test_a_tilde_in_a_home_variable_follows_the_home_it_sets(world, variable, value):
    """Each of these reaches this store only if `~` is read from the home's own HOME."""
    root = resolved_operator_root(load_config())
    alternate = world / "alternate"
    custom = alternate / ".config" / "imprint" / "custom.json"
    custom.parent.mkdir(parents=True)
    custom.write_text(json.dumps({"data_root": str(root.parent)}), encoding="utf-8")
    root.parent.mkdir(parents=True)
    (alternate / "data").symlink_to(root.parent)
    home = world / MARKER / "home"
    _hooked(home, env={"HOME": str(alternate), variable: value})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"


def test_a_home_that_names_the_home_it_already_has_is_not_flagged(world):
    home = world / MARKER / "home"
    _hooked(home, env={"HOME": str(world / "home")})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "same"


def test_a_config_with_a_relative_path_leaves_the_store_unknown(world, monkeypatch):
    """A hook reads it from its own folder, which cannot be known from here."""
    home = world / MARKER / "home"
    (world / "relative.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(world)
    _hooked(home, env={"IMPRINT_CONFIG": "relative.json"})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "unknown"


def test_a_disabled_instance_cannot_split_the_store_of_a_home_that_runs(world):
    home = world / MARKER / "home"
    _hooked(home)
    environment = [{"name": "IMPRINT_CONFIG", "value": str(_other_store(world))}]
    _t3_settings(world, claudeAgent=_instance(home), claudeAgent_off=_instance(home, enabled=False, environment=environment))
    section = _homes(world)
    (row,) = section["homes"]
    assert row["instances"] == ["claudeAgent", "claudeAgent_off"]
    assert row["data_root"] == "same"
    assert section["homes_different_data_root"] == 0


def test_a_disabled_instance_cannot_make_a_home_that_runs_unknown(world):
    home = world / MARKER / "home"
    _hooked(home)
    broken = world / "broken.json"
    broken.write_text("{", encoding="utf-8")
    environment = [{"name": "IMPRINT_CONFIG", "value": str(broken)}]
    _t3_settings(world, claudeAgent=_instance(home), claudeAgent_off=_instance(home, enabled=False, environment=environment))
    section = _homes(world)
    assert section["homes"][0]["data_root"] == "same"
    assert section["homes_unreadable"] == 0


def test_a_home_where_nothing_runs_still_shows_where_it_points(world):
    home = world / MARKER / "home"
    _hooked(home)
    environment = [{"name": "IMPRINT_CONFIG", "value": str(_other_store(world))}]
    _t3_settings(
        world, claudeAgent=_instance(world / MARKER / "off", enabled=False),
        claudeAgent_a=_instance(home, enabled=False), claudeAgent_b=_instance(home, enabled=False, environment=environment),
    )
    section = _homes(world)
    assert _by_instance(section)["claudeAgent_a"]["data_root"] == "different"
    assert section["homes_different_data_root"] == 0


def test_a_project_enabled_instance_counts_on_a_shared_home(world):
    home = world / MARKER / "home"
    _hooked(home)
    environment = [{"name": "IMPRINT_CONFIG", "value": str(_other_store(world))}]
    userdata = world / "t3" / "userdata"
    userdata.mkdir(parents=True)
    (userdata / "settings.json").write_text(json.dumps({
        "providerInstances": {
            "claudeAgent": _instance(home),
            "claudeAgent_project": _instance(home, enabled=False, environment=environment),
        },
        "projectSettingsOverrides": {"project-a": {"providerInstances": {"claudeAgent_project": {"enabled": True}}}},
    }), encoding="utf-8")
    section = _homes(world)
    assert section["homes"][0]["data_root"] == "different"
    assert section["homes_different_data_root"] == 1


def test_a_disabled_instance_is_listed_and_not_counted(world):
    bare, off = world / MARKER / "bare", world / MARKER / "off"
    _write_settings(bare, {})
    _write_settings(off, {})
    _t3_settings(
        world, claudeAgent=_instance(bare), claudeAgent_off=_instance(off, enabled=False),
        claudeAgent_cfg=_instance(world / MARKER / "cfg", config={"enabled": False}),
    )
    section = _homes(world)
    rows = _by_instance(section)
    assert rows["claudeAgent_off"]["enabled"] is False
    assert rows["claudeAgent_cfg"]["enabled"] is False
    assert section["homes_missing_hooks"] == 1


def test_a_project_can_turn_a_disabled_instance_back_on(world):
    bare = world / MARKER / "bare"
    _write_settings(bare, {})
    userdata = world / "t3" / "userdata"
    userdata.mkdir(parents=True)
    (userdata / "settings.json").write_text(json.dumps({
        "providerInstances": {
            "claudeAgent": _instance(world / MARKER / "off", enabled=False),
            "claudeAgent_project": _instance(bare, enabled=False),
        },
        "projectSettingsOverrides": {
            "project-a": {"providerInstances": {"claudeAgent_project": {"enabled": True}}},
            "project-b": {"providerInstances": {"claudeAgent": {"enabled": False}}},
            "project-c": "not an object",
            "project-d": {"providerInstances": "not an object"},
        },
    }), encoding="utf-8")
    section = _homes(world)
    rows = _by_instance(section)
    assert rows["claudeAgent_project"]["enabled"] is True
    assert rows["claudeAgent"]["enabled"] is False
    assert section["homes_missing_hooks"] == 1


def test_instances_on_one_home_are_one_row_even_through_a_symlink(world):
    home = world / MARKER / "home"
    _hooked(home)
    link = world / MARKER / "alias"
    link.symlink_to(home)
    _t3_settings(world, claudeAgent=_instance(home), claudeAgent_b=_instance(link), claudeAgent_c=_instance(f"  {home}  "))
    (row,) = _homes(world)["homes"]
    assert row["instances"] == ["claudeAgent", "claudeAgent_b", "claudeAgent_c"]


def test_one_instance_on_a_shared_home_can_split_its_store(world):
    home = world / MARKER / "home"
    _hooked(home)
    environment = [{"name": "IMPRINT_CONFIG", "value": str(_other_store(world))}]
    _t3_settings(world, claudeAgent=_instance(home), claudeAgent_b=_instance(home, environment=environment))
    (row,) = _homes(world)["homes"]
    assert row["data_root"] == "different"


def test_the_default_home_is_used_when_an_instance_names_none(world):
    _hooked(world / "home" / ".claude")
    _t3_settings(world, claudeAgent=_instance())
    assert _by_instance(_homes(world))["claudeAgent"]["hooks"] == "complete"


def test_t3_adds_a_default_instance_when_the_file_has_none(world):
    other = world / MARKER / "other"
    _hooked(other)
    _write_settings(world / "home" / ".claude", {})
    _t3_settings(world, claudeAgent_a=_instance(other), codex={"driver": "codex"})
    rows = _by_instance(_homes(world))
    assert rows["claudeAgent"]["hooks"] == "none"
    assert rows["claudeAgent_a"]["hooks"] == "complete"
    assert "codex" not in rows


def test_an_instance_configured_elsewhere_keeps_its_home(world):
    bare = world / MARKER / "bare"
    _write_settings(bare, {})
    _t3_settings(world, claudeAgent=_instance(bare, driver="codex"))
    assert _homes(world)["homes"] == []


def test_a_home_named_by_its_instance_environment_is_used(world):
    home = world / MARKER / "home"
    _hooked(home)
    _t3_settings(world, claudeAgent={
        "driver": "claudeAgent", "environment": [{"name": "CLAUDE_CONFIG_DIR", "value": str(home)}],
    })
    assert _by_instance(_homes(world))["claudeAgent"]["hooks"] == "complete"


@pytest.mark.parametrize("value", ["~/tilde-home", "~"])
def test_a_tilde_in_an_instance_claude_config_dir_is_expanded(world, value):
    """T3 expands a configured value. Only one the server inherited stays literal."""
    _hooked(world / "home" / "tilde-home")
    _hooked(world / "home")
    _t3_settings(world, claudeAgent={
        "driver": "claudeAgent", "environment": [{"name": "CLAUDE_CONFIG_DIR", "value": value}],
    })
    section = _homes(world)
    assert _by_instance(section)["claudeAgent"]["hooks"] == "complete"
    assert section["homes_unreadable"] == 0


def _moved_instance(alternate, **extra):
    """An instance that moves HOME, which T3 passes on to the Claude it starts."""
    environment = [{"name": "HOME", "value": str(alternate)}]
    return {"driver": "claudeAgent", "environment": environment, **extra}


def test_an_instance_that_moves_home_is_read_from_that_home(world):
    """The server's hooks do not run for it, so they must not read as complete."""
    alternate = world / "alternate"
    _write_settings(alternate / ".claude", {})
    _hooked(world / "home" / ".claude")
    _t3_settings(world, claudeAgent=_moved_instance(alternate))
    section = _homes(world)
    assert _by_instance(section)["claudeAgent"]["hooks"] == "none"
    assert section["homes_missing_hooks"] == 1


def test_an_instance_that_moves_home_gets_its_own_row(world):
    alternate = world / "alternate"
    _hooked(alternate / ".claude")
    _write_settings(world / "home" / ".claude", {})
    _t3_settings(world, claudeAgent=_instance(), claudeAgent_moved=_moved_instance(alternate))
    section = _homes(world)
    assert len(section["homes"]) == 2
    assert _by_instance(section)["claudeAgent"]["hooks"] == "none"
    assert _by_instance(section)["claudeAgent_moved"]["hooks"] == "complete"


@pytest.mark.parametrize("field", ["homePath", "CLAUDE_CONFIG_DIR"])
def test_a_named_home_wins_over_an_instance_home_and_keeps_the_server_tilde(world, field):
    """T3 expands `~` with its own home, so the instance's HOME does not move it."""
    alternate = world / "alternate"
    _write_settings(alternate / "tilde-home", {})
    _hooked(world / "home" / "tilde-home")
    if field == "homePath":
        instance = _moved_instance(alternate, config={"homePath": "~/tilde-home"})
    else:
        instance = _moved_instance(alternate)
        instance["environment"].append({"name": field, "value": "~/tilde-home"})
    _t3_settings(world, claudeAgent=instance)
    assert _by_instance(_homes(world))["claudeAgent"]["hooks"] == "complete"


@pytest.mark.parametrize("value", ["relative/home", "~/alternate"])
def test_an_instance_home_that_is_not_absolute_is_unreadable(world, value):
    """T3 expands no `~` in HOME, so Claude would get a relative home from either."""
    _hooked(world / "home" / ".claude")
    _t3_settings(world, claudeAgent=_moved_instance(value))
    section = _homes(world)
    assert _by_instance(section)["claudeAgent"]["hooks"] == "unreadable"
    assert section["homes_unreadable"] == 1


def test_a_tilde_in_a_home_path_is_expanded(world):
    _hooked(world / "home" / "tilde-home")
    _t3_settings(world, claudeAgent={"driver": "claudeAgent", "config": {"homePath": "~/tilde-home"}})
    assert _by_instance(_homes(world))["claudeAgent"]["hooks"] == "complete"


@pytest.mark.parametrize("path", ["relative/home", "~no-such-user-synthetic/home", 7])
def test_a_home_path_t3_could_not_use_is_unreadable(world, path):
    _t3_settings(world, claudeAgent={"driver": "claudeAgent", "config": {"homePath": path}})
    section = _homes(world)
    assert _by_instance(section)["claudeAgent"]["hooks"] == "unreadable"
    assert section["homes_unreadable"] == 1


def _bad_json(path):
    path.write_text("{", encoding="utf-8")


def _not_object(path):
    path.write_text("[]", encoding="utf-8")


def _too_deep(path):
    path.write_text("[" * 200_000, encoding="utf-8")


def _not_utf8(path):
    path.write_bytes(b'{"hooks": "\xff"}')


def _byte_order_mark(path):
    path.write_bytes(b"\xef\xbb\xbf{}")


def _utf_16(path):
    path.write_bytes("{}".encode("utf-16"))


def _utf_16_without_mark(path):
    path.write_bytes("{}".encode("utf-16-le"))


def _utf_32(path):
    path.write_bytes("{}".encode("utf-32"))


def _fifo(path):
    os.mkfifo(path)


def _directory(path):
    path.mkdir()


def _too_large(path):
    path.write_text(json.dumps({"padding": "x" * (4 * 1024 * 1024)}), encoding="utf-8")


ENCODINGS = [_byte_order_mark, _utf_16, _utf_16_without_mark, _utf_32]
ENCODING_IDS = ["byte order mark", "utf-16", "utf-16 without a mark", "utf-32"]
UNREADABLE = pytest.mark.parametrize(
    "damage", [_bad_json, _not_object, _too_deep, _not_utf8, *ENCODINGS, _fifo, _directory, _too_large],
    ids=["bad json", "not an object", "too deep", "not utf-8", *ENCODING_IDS, "fifo", "directory", "too large"],
)


@UNREADABLE
def test_a_config_that_cannot_be_read_leaves_the_store_unknown(world, damage):
    home = world / MARKER / "home"
    config = world / "broken.json"
    damage(config)
    _hooked(home, env={"IMPRINT_CONFIG": str(config)})
    _t3_settings(world, claudeAgent=_instance(home))
    section = _without_blocking(lambda: _homes(world))
    assert _by_instance(section)["claudeAgent"]["data_root"] == "unknown"
    assert section["homes_unreadable"] == 1
    assert section["homes_different_data_root"] == 0


@pytest.mark.parametrize("damage", ENCODINGS, ids=ENCODING_IDS)
def test_an_encoding_no_hook_can_load_leaves_the_store_unknown(world, damage):
    """json.loads decodes these from bytes, and a hook, which reads text, fails on them."""
    home = world / MARKER / "home"
    config = world / "encoded.json"
    damage(config)
    with pytest.raises((ValidationError, UnicodeDecodeError)):
        load_config(config)
    _hooked(home, env={"IMPRINT_CONFIG": str(config)})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["data_root"] == "unknown"


def test_one_bad_config_keeps_the_rows_of_every_other_home(world):
    """A selector mode of the wrong type raises TypeError from the config check."""
    good, bare, bad = world / MARKER / "good", world / MARKER / "bare", world / MARKER / "bad"
    broken = world / "unhashable.json"
    broken.write_text(json.dumps({"selector": {"mode": ["jev"]}}), encoding="utf-8")
    _hooked(good)
    _write_settings(bare, {})
    _hooked(bad, env={"IMPRINT_CONFIG": str(broken)})
    _t3_settings(world, claudeAgent=_instance(good), claudeAgent_bare=_instance(bare), claudeAgent_bad=_instance(bad))
    section = _homes(world)
    rows = _by_instance(section)
    assert section["state"] == "read"
    assert rows["claudeAgent"]["data_root"] == "same"
    assert rows["claudeAgent_bad"]["hooks"] == "complete"
    assert rows["claudeAgent_bad"]["data_root"] == "unknown"
    assert section["homes_missing_hooks"] == 1
    assert section["homes_unreadable"] == 1


def test_one_home_too_deep_to_read_keeps_the_rows_of_every_other_home(world):
    """Some Python versions parse this file and only fail on the command inside it."""
    bare, deep = world / MARKER / "bare", world / MARKER / "deep"
    _write_settings(bare, {})
    deep.mkdir(parents=True)
    depth = 100_000
    command = "[" * depth + "]" * depth
    (deep / "settings.json").write_text(
        '{"hooks": {"Stop": [{"hooks": [{"command": ' + command + "}]}]}}", encoding="utf-8",
    )
    _t3_settings(world, claudeAgent=_instance(bare), claudeAgent_deep=_instance(deep))
    section = _homes(world)
    rows = _by_instance(section)
    assert section["state"] == "read"
    assert rows["claudeAgent"]["hooks"] == "none"
    assert rows["claudeAgent_deep"]["hooks"] == "unreadable"
    assert section["homes_missing_hooks"] == 1
    assert section["homes_unreadable"] == 1


def test_a_home_nested_past_the_limit_is_unreadable_on_every_python(world):
    """Every Python parses and prints this depth, so the answer cannot rest on the stack."""
    home = world / MARKER / "deep"
    home.mkdir(parents=True)
    depth = 100
    command = "[" * depth + "]" * depth
    (home / "settings.json").write_text(
        '{"hooks": {"Stop": [{"hooks": [{"command": ' + command + "}]}]}}", encoding="utf-8",
    )
    _t3_settings(world, claudeAgent=_instance(home))
    section = _homes(world)
    assert _by_instance(section)["claudeAgent"]["hooks"] == "unreadable"
    assert section["homes_missing_hooks"] == 0
    assert section["homes_unreadable"] == 1


def test_a_hook_check_that_fails_keeps_the_rest_of_the_row(world, monkeypatch):
    home = world / MARKER / "home"
    _hooked(home)
    _t3_settings(world, claudeAgent=_instance(home))

    def broken(settings):
        raise RecursionError("synthetic")

    monkeypatch.setattr(t3_homes, "_hook_state", broken)
    section = _homes(world)
    assert section["state"] == "read"
    assert _by_instance(section)["claudeAgent"]["hooks"] == "unreadable"
    assert _by_instance(section)["claudeAgent"]["data_root"] == "same"


@UNREADABLE
def test_an_unreadable_t3_settings_file_is_reported_and_does_not_fail(world, damage):
    userdata = world / "t3" / "userdata"
    userdata.mkdir(parents=True)
    damage(userdata / "settings.json")
    report = _report(world)
    section = report["metrics"]["t3_homes"]
    assert (section["state"], section["homes"]) == ("settings_unreadable", [])
    assert report["degraded_reasons"] == _report(world, {})["degraded_reasons"]


def test_a_missing_t3_settings_file_is_reported_and_does_not_fail(world):
    report = _report(world)
    section = report["metrics"]["t3_homes"]
    assert (section["state"], section["homes"]) == ("settings_missing", [])
    assert section["homes_missing_hooks"] == 0
    assert report["degraded_reasons"] == _report(world, {})["degraded_reasons"]


@pytest.mark.parametrize("instances", [["not", "an", "object"], {"not a slug": {"driver": "claudeAgent"}}], ids=["list", "id"])
def test_instances_t3_itself_would_refuse_are_unreadable_settings(world, instances):
    userdata = world / "t3" / "userdata"
    userdata.mkdir(parents=True)
    (userdata / "settings.json").write_text(json.dumps({"providerInstances": instances}), encoding="utf-8")
    assert _homes(world)["state"] == "settings_unreadable"


def test_a_t3_home_that_cannot_be_resolved_is_unreadable(world, monkeypatch):
    monkeypatch.setenv("T3CODE_HOME", "~no-such-user-synthetic/t3")
    assert _homes(world)["state"] == "settings_unreadable"


def test_an_unexpected_failure_is_reported_as_unreadable(world, monkeypatch):
    def broken(root):
        raise KeyError("synthetic")

    monkeypatch.setattr(t3_homes, "_read_homes", broken)
    assert _homes(world)["state"] == "settings_unreadable"


@UNREADABLE
def test_a_home_whose_settings_cannot_be_read_is_not_called_hookless(world, damage):
    home = world / MARKER / "home"
    home.mkdir(parents=True)
    damage(home / "settings.json")
    _t3_settings(world, claudeAgent=_instance(home))
    section = _homes(world)
    assert section["state"] == "read"
    assert _by_instance(section)["claudeAgent"]["hooks"] == "unreadable"
    assert _by_instance(section)["claudeAgent"]["data_root"] == "unknown"
    assert section["homes_unreadable"] == 1
    assert section["homes_missing_hooks"] == 0


@pytest.mark.parametrize("hooks", [["a list"], "text"], ids=["list", "text"])
def test_a_home_with_a_hooks_value_of_another_shape_is_unreadable(world, hooks):
    home = world / MARKER / "home"
    _write_settings(home, {"hooks": hooks})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["hooks"] == "unreadable"


@pytest.mark.parametrize("hooks", [
    None, {}, {"Stop": "not a list"}, {"Stop": ["not an entry"]}, {"Stop": [{"hooks": "not a list"}]},
    {"Stop": [{"hooks": ["not a hook"]}]}, {"Stop": [{"hooks": [{"command": "another-tool"}]}]},
], ids=["null", "empty", "event", "entry", "entry hooks", "hook", "unrelated"])
def test_hooks_of_any_shape_that_are_not_imprints_read_as_none(world, hooks):
    home = world / MARKER / "home"
    _write_settings(home, {"hooks": hooks})
    _t3_settings(world, claudeAgent=_instance(home))
    assert _by_instance(_homes(world))["claudeAgent"]["hooks"] == "none"


def test_with_the_setting_off_nothing_is_read_and_nothing_is_reported(world, monkeypatch):
    reads = []
    monkeypatch.setattr(t3_origin, "t3_settings_path", lambda environ=None: reads.append(environ))
    monkeypatch.setattr(t3_homes, "t3_settings_path", lambda environ=None: reads.append(environ))
    home = world / MARKER / "home"
    _write_settings(home, {})
    _t3_settings(world, claudeAgent=_instance(home))
    report = _report(world, {"t3_capture_origin": False})
    assert "t3_homes" not in report["metrics"]
    assert reads == []
    assert set(report["metrics"]) == set(_report(world, {})["metrics"])
