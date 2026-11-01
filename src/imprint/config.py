"""Portable public configuration. Private runtime state is never packaged."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ValidationError
from .paths import default_data_root, expand_home, home_directory, operator_root, validate_data_root

# Dotted, so a build that predates them keeps them without reading them and a
# rollback needs no change to the config. The selector object is closed, so a key
# there would have to be removed first.
SHORT_LINES_PROMPT = "org.imprint.short_lines.prompt"
SHORT_LINES_SESSION_START = "org.imprint.short_lines.session_start"

DEFAULTS = {
    "config_version": "3.0.0",
    "operator_slug": "default",
    "node_id": "primary",
    "compiler": True,
    # Reads T3's own message records to verify who wrote a prompt. Off by
    # default: an installation opts in to trusting that local record.
    "t3_capture_origin": False,
    # Deliver each route's records on short lines. Separate switches, because
    # session start is not filtered by client and a wider one reaches further.
    SHORT_LINES_PROMPT: False,
    SHORT_LINES_SESSION_START: False,
    "context_budget_bytes": 32768,
    "allow_higher_budget": False,
    "spool_retention_days": 30,
    "domains": [],
    "experimental": {"digest": False, "profile_learning": False},
    # Optional relevance selection for prompt-time retrieval. The shipped mode
    # is deterministic: no network request is possible until an installation
    # opts in to "jev" or "shadow" in its own config.
    "selector": {
        "mode": "deterministic",
        "jev": {
            "endpoint": "https://api.typesafe.ai/v1/systemone",
            "model": "jev-1.13.0",
            "api_key_env": "TYPESAFE_API_KEY",
            "api_key_file": None,
            "top": 20,
            "score_floor": 0.55,
            "timeout_seconds": 3,
            "prefilter_keep": 150,
            "budget_bytes": None,
            "node_types": ["Verdict", "Principle", "Belief", "Value", "Rule", "Pattern"],
            "exclude_domains": [],
            "short_prompt_context": {"enabled": False, "below_chars": 200, "max_chars": 1500},
            "stale_before": None,
            "standing_rules": {"enabled": False, "score_floor": 0.25, "rare_word_share": 0.15, "slots": 2, "keep": 30},
            "log_decisions": True,
        },
    },
}

SELECTOR_MODES = frozenset({"deterministic", "jev", "shadow"})
SELECTOR_ENV = "IMPRINT_SELECTOR"
# Environment values that leave the configured selector mode in force. Every
# other non-empty value is read as "off", so a mistyped kill switch fails safe.
_SELECTOR_ENV_KEEPS_CONFIG = frozenset({"jev", "shadow"})
# Most characters of turn text a remote selector may receive. Defined here,
# not in the selector module, so hook code can bound the text without
# importing anything that is able to reach the network.
SELECTOR_STATE_LIMIT = 8000
# Judgment-partition record types whose own statement can be sent to a remote
# selector. Business, observation, outcome, self-model, direction and
# ingested records are not selectable, whatever the config says.
SELECTOR_NODE_TYPES = frozenset({"Verdict", "Principle", "Belief", "Value", "Rule", "Pattern"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")

# Keys the loader recognizes. ``data_root`` and ``hooks_dir`` are written by the
# installers but are not part of the portable defaults. Unknown keys are rejected
# unless namespaced with a dot, which reserves an extension space the loader
# preserves but does not interpret.
KNOWN_TOP_LEVEL = frozenset(DEFAULTS) | {"data_root", "hooks_dir"}
SAFE_LOCAL_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def _is_int(value: Any) -> bool:
    """JSON booleans are Python integers, but never valid numeric settings."""
    return isinstance(value, int) and not isinstance(value, bool)


def effective_selector_mode(selector: Any) -> str:
    """Resolve the selector mode. The environment can only turn it off.

    Any non-empty ``IMPRINT_SELECTOR`` other than ``jev`` or ``shadow`` (for
    example ``off``, ``deterministic``, ``0``, ``false``, or ``disabled``)
    forces deterministic retrieval whatever the config says. ``jev`` and
    ``shadow`` leave the configured mode in force: no environment value can
    enable a network selector that the config leaves deterministic.
    """
    override = os.environ.get(SELECTOR_ENV, "").strip().lower()
    if override and override not in _SELECTOR_ENV_KEEPS_CONFIG:
        return "deterministic"
    mode = selector.get("mode") if isinstance(selector, dict) else None
    return mode if mode in {"jev", "shadow"} else "deterministic"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _merge_selector(defaults: dict[str, Any], loaded: Any) -> Any:
    """Overlay a partial selector object on its defaults, one level per object.

    Unknown keys are carried through so validation can reject them instead of
    silently dropping a misspelled setting. Non-objects pass through unchanged
    for the same reason.
    """
    if not isinstance(loaded, dict):
        return loaded
    merged = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_selector(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _validate_selector(data: dict[str, Any]) -> None:
    selector = data.get("selector")
    if not isinstance(selector, dict) or set(selector) != set(DEFAULTS["selector"]):
        raise ValidationError("selector must contain exactly mode and jev")
    if selector["mode"] not in SELECTOR_MODES:
        raise ValidationError("selector mode must be deterministic, jev, or shadow")
    jev = selector["jev"]
    expected = DEFAULTS["selector"]["jev"]
    if not isinstance(jev, dict) or set(jev) != set(expected):
        raise ValidationError("selector jev contains unknown or missing fields")
    endpoint = jev["endpoint"]
    if not isinstance(endpoint, str) or any(ch.isspace() or ord(ch) < 32 for ch in endpoint):
        raise ValidationError("selector jev endpoint must be an https:// URL")
    try:
        parts = urlsplit(endpoint)
        parts.port  # raises ValueError for a malformed port
    except ValueError as exc:
        raise ValidationError("selector jev endpoint must be an https:// URL") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        # Credentials travel only in the Authorization header, never in a URL.
        raise ValidationError("selector jev endpoint must be an https:// URL without credentials")
    if not isinstance(jev["model"], str) or _MODEL_NAME.fullmatch(jev["model"]) is None:
        raise ValidationError("selector jev model must be a model name")
    if not isinstance(jev["api_key_env"], str) or _ENV_NAME.fullmatch(jev["api_key_env"]) is None:
        raise ValidationError("selector jev api_key_env must be an environment variable name")
    key_file = jev["api_key_file"]
    if key_file is not None:
        if not isinstance(key_file, str) or not key_file.strip() or "\x00" in key_file:
            raise ValidationError("selector jev api_key_file must be null or an absolute path")
        if not Path(key_file).expanduser().is_absolute():
            raise ValidationError("selector jev api_key_file must be null or an absolute path")
    if not _is_int(jev["top"]) or not 1 <= jev["top"] <= 150:
        raise ValidationError("selector jev top must be 1..150")
    if not _is_number(jev["score_floor"]) or not 0 <= jev["score_floor"] <= 1:
        raise ValidationError("selector jev score_floor must be 0..1")
    if not _is_number(jev["timeout_seconds"]) or not 0 < jev["timeout_seconds"] <= 30:
        raise ValidationError("selector jev timeout_seconds must be above 0 and at most 30")
    if not _is_int(jev["prefilter_keep"]) or not 1 <= jev["prefilter_keep"] <= 10000:
        raise ValidationError("selector jev prefilter_keep must be 1..10000")
    budget = jev["budget_bytes"]
    if budget is not None:
        # The selector budget obeys the same bounds as context_budget_bytes.
        if not _is_int(budget) or not 4096 <= budget <= 131072:
            raise ValidationError("selector jev budget_bytes must be null or 4096..131072")
        if budget > 32768 and data.get("allow_higher_budget") is not True:
            raise ValidationError("selector jev budget_bytes above 32768 requires allow_higher_budget=true")
    node_types = jev["node_types"]
    if (
        not isinstance(node_types, list) or not node_types
        or any(not isinstance(item, str) for item in node_types)
        or len(set(node_types)) != len(node_types)
        or set(node_types) - SELECTOR_NODE_TYPES
    ):
        raise ValidationError(
            f"selector jev node_types must be a non-empty unique subset of {sorted(SELECTOR_NODE_TYPES)}"
        )
    excluded = jev["exclude_domains"]
    if not isinstance(excluded, list) or any(
        not isinstance(item, str) or SAFE_LOCAL_ID.fullmatch(item) is None for item in excluded
    ):
        raise ValidationError("selector jev exclude_domains must be an array of safe domain ids")
    context = jev["short_prompt_context"]
    if not isinstance(context, dict) or set(context) != set(expected["short_prompt_context"]):
        raise ValidationError("selector jev short_prompt_context must contain enabled, below_chars, max_chars")
    if not isinstance(context["enabled"], bool):
        raise ValidationError("selector jev short_prompt_context enabled must be a boolean")
    if not _is_int(context["below_chars"]) or not 1 <= context["below_chars"] <= 2000:
        raise ValidationError("selector jev short_prompt_context below_chars must be 1..2000")
    if not _is_int(context["max_chars"]) or not 1 <= context["max_chars"] <= 4000:
        raise ValidationError("selector jev short_prompt_context max_chars must be 1..4000")
    stale_before = jev["stale_before"]
    if stale_before is not None and (not isinstance(stale_before, str) or _UTC_TIME.fullmatch(stale_before) is None):
        raise ValidationError("selector jev stale_before must be null or a UTC time like 2026-01-31T12:00:00Z")
    if stale_before is not None:
        try:
            datetime.fromisoformat(stale_before[:-1])
        except ValueError as exc:
            raise ValidationError("selector jev stale_before must be null or a UTC time like 2026-01-31T12:00:00Z") from exc
    standing = jev["standing_rules"]
    if not isinstance(standing, dict) or set(standing) != set(expected["standing_rules"]):
        raise ValidationError("selector jev standing_rules must contain enabled, score_floor, rare_word_share, slots, keep")
    if not isinstance(standing["enabled"], bool):
        raise ValidationError("selector jev standing_rules enabled must be a boolean")
    if not _is_number(standing["score_floor"]) or not 0 <= standing["score_floor"] <= 1:
        raise ValidationError("selector jev standing_rules score_floor must be 0..1")
    if not _is_number(standing["rare_word_share"]) or not 0 < standing["rare_word_share"] <= 1:
        raise ValidationError("selector jev standing_rules rare_word_share must be above 0 and at most 1")
    if not _is_int(standing["slots"]) or not 1 <= standing["slots"] <= 5:
        raise ValidationError("selector jev standing_rules slots must be 1..5")
    if not _is_int(standing["keep"]) or not 1 <= standing["keep"] <= 150:
        raise ValidationError("selector jev standing_rules keep must be 1..150")
    if standing["enabled"] and standing["keep"] >= jev["prefilter_keep"]:
        raise ValidationError("selector jev standing_rules keep must be below prefilter_keep")
    if not isinstance(jev["log_decisions"], bool):
        raise ValidationError("selector jev log_decisions must be a boolean")


def _validate_config(data: dict[str, Any]) -> None:
    if not isinstance(data.get("config_version"), str) or data["config_version"] != DEFAULTS["config_version"]:
        raise ValidationError("unsupported config_version")
    for field in ("operator_slug", "node_id"):
        value = data.get(field)
        if not isinstance(value, str) or SAFE_LOCAL_ID.fullmatch(value) is None:
            raise ValidationError(f"{field} must be a safe lowercase identifier")
    for field in ("compiler", "allow_higher_budget", "t3_capture_origin", SHORT_LINES_PROMPT, SHORT_LINES_SESSION_START):
        if not isinstance(data.get(field), bool):
            raise ValidationError(f"{field} must be a boolean")
    if not _is_int(data.get("context_budget_bytes")) or not 4096 <= data["context_budget_bytes"] <= 131072:
        raise ValidationError("context_budget_bytes must be 4096..131072")
    if data["context_budget_bytes"] > 32768 and data["allow_higher_budget"] is not True:
        raise ValidationError("context_budget_bytes above 32768 requires allow_higher_budget=true")
    if not _is_int(data.get("spool_retention_days")) or not 1 <= data["spool_retention_days"] <= 36500:
        raise ValidationError("spool_retention_days must be 1..36500")
    if not isinstance(data.get("domains"), list):
        raise ValidationError("domains must be an array")
    domain_fields = {"domain_id", "public_label", "safe_paths", "keywords", "frozen"}
    for domain in data["domains"]:
        if not isinstance(domain, dict) or not {"domain_id", "public_label"}.issubset(domain):
            raise ValidationError("each domain must be an object with domain_id and public_label")
        if set(domain) - domain_fields:
            raise ValidationError("domain config contains unknown fields")
        if not isinstance(domain["domain_id"], str) or SAFE_LOCAL_ID.fullmatch(domain["domain_id"]) is None:
            raise ValidationError("domain_id must be a safe lowercase identifier")
        if not isinstance(domain["public_label"], str) or not domain["public_label"].strip():
            raise ValidationError("domain public_label must be a non-empty string")
        for field in ("safe_paths", "keywords"):
            value = domain.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValidationError(f"domain {field} must be an array of strings")
        if "frozen" in domain and not isinstance(domain["frozen"], bool):
            raise ValidationError("domain frozen must be a boolean")
    experimental = data.get("experimental")
    expected_experimental = frozenset(DEFAULTS["experimental"])
    if not isinstance(experimental, dict) or set(experimental) != expected_experimental:
        raise ValidationError("experimental must contain exactly digest and profile_learning")
    if any(not isinstance(experimental[field], bool) for field in expected_experimental):
        raise ValidationError("experimental settings must be booleans")
    for field in ("data_root", "hooks_dir"):
        if field in data and (not isinstance(data[field], str) or not data[field].strip()):
            raise ValidationError(f"{field} must be a non-empty path string")
    if "hooks_dir" in data:
        hooks_dir = Path(data["hooks_dir"]).expanduser()
        if not hooks_dir.is_absolute() or "\x00" in data["hooks_dir"]:
            raise ValidationError("hooks_dir must be an absolute safe path")
    _validate_selector(data)


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("IMPRINT_CONFIG")
    if override:
        return expand_home(override, environ)
    if os.name == "nt":
        base = Path(values.get("APPDATA", home_directory(environ)))
        return base / "Imprint" / "config.json"
    return Path(values.get("XDG_CONFIG_HOME", home_directory(environ) / ".config")) / "imprint" / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    loaded: Any = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"corrupt config: {target}") from exc
    return parse_config(loaded)


def parse_config(loaded: Any) -> dict[str, Any]:
    """Validate a decoded config over the defaults, for a caller that read it itself."""
    if not isinstance(loaded, dict):
        raise ValidationError("config must be an object")
    unknown = {key for key in loaded if key not in KNOWN_TOP_LEVEL and "." not in key}
    if unknown:
        raise ValidationError(
            f"unknown config keys: {sorted(unknown)}; namespace extensions with a dot"
        )
    if "selector" in loaded:
        # A partial selector object overlays the defaults instead of replacing them.
        loaded["selector"] = _merge_selector(DEFAULTS["selector"], loaded["selector"])
    data = copy.deepcopy(DEFAULTS)
    data.update(loaded)
    _validate_config(data)
    try:
        from .domains import registry_from_config
        registry_from_config(data)
    except ValueError as exc:
        raise ValidationError(f"invalid domains config: {exc}") from exc
    return data


def resolved_operator_root(config: dict[str, Any], environ: Mapping[str, str] | None = None) -> Path:
    explicit = config.get("data_root")
    base = validate_data_root(Path(explicit), environ=environ) if explicit else default_data_root(environ)
    return operator_root(str(config["operator_slug"]), base)
