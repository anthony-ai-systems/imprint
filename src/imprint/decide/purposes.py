"""The closed list of purposes a typed decision may serve, and how each may send.

A purpose sends only when the config names it with a mode of ``shadow`` or
``enforce`` and a daily cap above zero. The shipped config names none, and an entry
this build cannot read (an unknown purpose or field, or a bad value) is off. ``shadow``
returns answers flagged so that the caller must not act on them; the module only
reports the mode, and each caller decides what that means for it.

Two environment variables can turn every purpose off and none can turn one on:
``IMPRINT_DECIDE`` set to any non-empty value, and ``IMPRINT_SELECTOR`` set to a
value that turns the selector off, so the switch that keeps prompts local keeps
these questions local too.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from imprint.config import (
    DECIDE_MODES,
    DECIDE_PURPOSES,
    DECIDE_PURPOSES_KEY,
    decide_purposes_enabled,
    decide_setting,
    selector_switched_off,
)

PURPOSES = DECIDE_PURPOSES
MODES = DECIDE_MODES
DECIDE_ENV = "IMPRINT_DECIDE"


def switched_off() -> bool:
    """Whether the environment turns every purpose off."""
    return bool(os.environ.get(DECIDE_ENV, "").strip()) or selector_switched_off()


def purpose_mode(config: Mapping[str, Any], purpose: str) -> tuple[str, int, str | None]:
    """``(mode, daily_cap, reason)``. ``reason`` says why the mode is ``off``.

    Reasons are closed: ``unknown_purpose``, ``switched_off`` and ``purpose_off``.
    """
    if purpose not in PURPOSES:
        return "off", 0, "unknown_purpose"
    if switched_off():
        return "off", 0, "switched_off"
    mode, cap = decide_setting(config, purpose)
    if mode == "off":
        return "off", 0, "purpose_off"
    return mode, cap, None


def enabled_purposes(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The purposes the config lets send: a mode other than ``off`` and a daily cap
    above 0. The environment can still turn them off; this ignores it."""
    return decide_purposes_enabled(config)


__all__ = [
    "DECIDE_ENV",
    "DECIDE_PURPOSES_KEY",
    "MODES",
    "PURPOSES",
    "enabled_purposes",
    "purpose_mode",
    "switched_off",
]
