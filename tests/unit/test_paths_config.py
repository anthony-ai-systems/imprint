from __future__ import annotations

from pathlib import Path

import pytest

from imprint.config import config_path, resolved_operator_root
from imprint.config import load_config
from imprint.errors import SafetyError, ValidationError
from imprint.paths import default_data_root, operator_root, validate_data_root


def test_explicit_data_root_with_spaces_is_validated_and_supported(tmp_path):
    base = tmp_path / "local data with spaces"
    assert resolved_operator_root({"data_root": str(base), "operator_slug": "operator-a"}) == base / "operator-a"


@pytest.mark.parametrize("marker", ["Dropbox", "OneDrive", "CloudStorage", "Google Drive"])
def test_explicit_sync_root_is_refused(tmp_path, marker):
    with pytest.raises(SafetyError, match="Cloud-sync"):
        resolved_operator_root({"data_root": str(tmp_path / marker / "Imprint"), "operator_slug": "operator-a"})


def test_home_as_explicit_data_root_is_refused():
    with pytest.raises(SafetyError, match="root or home"):
        resolved_operator_root({"data_root": str(Path.home()), "operator_slug": "operator-a"})


def test_higher_context_budget_requires_explicit_bounded_opt_in(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"context_budget_bytes":40000}')
    with pytest.raises(ValidationError, match="allow_higher_budget"):
        load_config(config)
    config.write_text('{"context_budget_bytes":40000,"allow_higher_budget":true}')
    assert load_config(config)["context_budget_bytes"] == 40000
    config.write_text('{"context_budget_bytes":131073,"allow_higher_budget":true}')
    with pytest.raises(ValidationError, match="4096..131072"):
        load_config(config)


def test_unknown_bare_config_key_is_rejected_but_namespaced_is_kept(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"spool_retention_dayz":7}')
    with pytest.raises(ValidationError, match="unknown config keys"):
        load_config(config)
    config.write_text('{"org.example.ext":{"anything":true}}')
    loaded = load_config(config)
    assert loaded["org.example.ext"] == {"anything": True}


def test_unsupported_config_version_is_rejected(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"config_version":"2.0.0"}')
    with pytest.raises(ValidationError, match="config_version"):
        load_config(config)


def _variables(tmp_path, **extra):
    """Name another user's home in a mapping, which this process's own does not match."""
    home = tmp_path / "another-home"
    return home, {"HOME": str(home), **extra}


def test_supplied_variables_decide_the_default_config_path(tmp_path):
    home, variables = _variables(tmp_path)
    assert config_path(variables) == home / ".config" / "imprint" / "config.json"


def test_supplied_variables_decide_the_default_data_root(tmp_path):
    home, variables = _variables(tmp_path)
    assert default_data_root(variables) == (home / ".local" / "share" / "imprint").resolve()


def test_a_tilde_in_a_supplied_variable_uses_the_supplied_home(tmp_path):
    home, variables = _variables(tmp_path, IMPRINT_CONFIG="~/custom.json", IMPRINT_DATA_ROOT="~/data")
    assert config_path(variables) == home / "custom.json"
    assert default_data_root(variables) == (home / "data").resolve()


def test_a_tilde_in_a_config_data_root_uses_the_supplied_home(tmp_path):
    home, variables = _variables(tmp_path)
    config = {"data_root": "~/data", "operator_slug": "operator-a"}
    assert resolved_operator_root(config, variables) == (home / "data").resolve() / "operator-a"


def test_a_supplied_home_is_the_home_the_data_root_may_not_be(tmp_path):
    home, variables = _variables(tmp_path, IMPRINT_DATA_ROOT=str(tmp_path / "another-home"))
    with pytest.raises(SafetyError):
        default_data_root(variables)


def test_with_no_supplied_home_the_process_home_still_decides(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "process-home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert config_path({}) == tmp_path / "process-home" / ".config" / "imprint" / "config.json"
    assert default_data_root({}) == (tmp_path / "process-home" / ".local" / "share" / "imprint").resolve()
    assert config_path({"IMPRINT_CONFIG": "~/custom.json"}) == tmp_path / "process-home" / "custom.json"


@pytest.mark.parametrize("home", ["~/alt", "~synthetic", "~root"])
@pytest.mark.parametrize("call", [
    config_path,
    default_data_root,
    lambda: operator_root("operator-a"),
    lambda: validate_data_root(Path("~/x")),
    lambda: resolved_operator_root({"data_root": "~/x", "operator_slug": "operator-a"}),
], ids=["config path", "data root", "operator root", "validate", "config data root"])
def test_with_no_variables_given_expanduser_still_decides_a_tilde(monkeypatch, home, call):
    """Path.home() takes these on some Python versions, and a real hook passes no variables."""
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("IMPRINT_CONFIG", "~/c.json")
    monkeypatch.setenv("IMPRINT_DATA_ROOT", "~/x")
    with pytest.raises(RuntimeError, match="home directory"):
        call()
