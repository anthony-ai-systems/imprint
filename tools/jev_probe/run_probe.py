#!/usr/bin/env python3
"""Run the 24-call synthetic Jev probe through ``imprint.decide`` as purpose ``probe``.

By default this prints the plan and each request's size, and sends nothing. It sends
only when every one of these holds, and otherwise refuses before the first call:

- ``--send-approved-calls N`` is given and N equals the plan's 24 calls;
- ``--config PATH`` names a config whose ``org.imprint.decide.purposes`` enables
  ``probe`` (mode ``shadow`` or ``enforce``) with a daily cap that leaves room for
  all 24 calls today, and neither ``IMPRINT_DECIDE`` nor ``IMPRINT_SELECTOR`` turns
  it off;
- a key that each call would accept is where ``selector.jev`` says to look.

It stops at the first call that does not come back answered, and never retries.

The plan:

- Calls 1-12 measure how cost and latency grow: two synthetic states (about 100 and
  about 1,000 tokens) times three Noul counts (1, 10, 150) times two repeats,
  interleaved.
- Calls 13-24 compare the primitives: twelve labelled cases (four applicable, four
  unrelated, four conflicting or insufficient), each asked as one Noul, one Choice and
  one Score in the same request.

Every state and statement is invented and lives in ``cases.json`` beside this file.
The model is pinned to ``jev-1.13.0``. Output lines hold counts, sizes, usage,
latency, answer shapes, probabilities and case ids, and no text.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from imprint.config import load_config, resolved_operator_root
from imprint.decide import ask, calls_today, choice, encode_request, noul, purpose_mode, score
from imprint.retrieve.jev import JevSettings, resolve_api_key

PURPOSE = "probe"
MODEL = "jev-1.13.0"
PLANNED_CALLS = 24
TIMEOUT_SECONDS = 10
CASES = Path(__file__).resolve().parent / "cases.json"
NOUL_PREFIX = "Does this judgment apply to the current turn? Judgment: "
CHOICE_PREFIX = "How does this judgment bear on the current turn? Judgment: "
SCORE_PREFIX = "What would following this judgment do to the agent's next action? Judgment: "
OPTIONS = {
    "required_here": "The judgment applies to this turn and the agent should follow it now.",
    "conflicts_with_higher_instruction": (
        "The judgment would apply, but the turn holds a later or higher instruction that overrides it."
    ),
    "irrelevant": "The judgment is about something this turn does not touch.",
    "not_enough_evidence": "The turn does not say enough to tell whether the judgment applies.",
}
LEVELS = (
    "No effect on what the agent does next.",
    "Useful background; the next action stays the same.",
    "Changes what the agent does next.",
    "Prevents a consequential error.",
)
NOUL_COUNTS = (1, 10, 150)
STATE_LENGTHS = ("short", "long")
REPEATS = (1, 2)


def load_cases(path: Path = CASES) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def noul_bank(cases: Mapping[str, Any]) -> list[str]:
    bank = cases["noul_bank"]
    return [template.format(x=item) + "." for template in bank["templates"] for item in bank["objects"]]


def plan(cases: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The 24 calls, in order. Each holds a state and named questions."""
    cases = cases or load_cases()
    states = {"short": cases["states"]["short"], "long": "\n\n".join(cases["states"]["long"])}
    bank = noul_bank(cases)
    calls: list[dict[str, Any]] = []
    for repeat in REPEATS:
        for count in NOUL_COUNTS:
            for length in STATE_LENGTHS:
                calls.append({
                    "kind": "scaling", "case": f"{length}-{count}-r{repeat}", "label": None, "state_kind": length,
                    "state": states[length],
                    "questions": {f"q{index:03d}": noul(NOUL_PREFIX + bank[index]) for index in range(count)},
                })
    for case in cases["primitive_cases"]:
        judgment = case["judgment"]
        calls.append({
            "kind": "primitives", "case": case["id"], "label": case["label"], "state_kind": "case",
            "state": case["turn"],
            "questions": {
                "applies": noul(NOUL_PREFIX + judgment),
                "relation": choice(CHOICE_PREFIX + judgment, OPTIONS),
                "impact": score(SCORE_PREFIX + judgment, LEVELS),
            },
        })
    for number, item in enumerate(calls, 1):
        item["call"] = number
    return calls


def _types(questions: Mapping[str, Any]) -> dict[str, int]:
    return dict(sorted(Counter(question.type for question in questions.values()).items()))


def describe(item: Mapping[str, Any]) -> dict[str, object]:
    """One plan line: no text, only ids, kinds and sizes."""
    return {
        "call": item["call"], "kind": item["kind"], "case": item["case"], "label": item["label"],
        "state_kind": item["state_kind"], "state_chars": len(item["state"]),
        "questions": len(item["questions"]), "types": _types(item["questions"]),
        "request_bytes": len(encode_request(MODEL, item["state"], item["questions"])),
    }


def refusals(args: argparse.Namespace, calls: list[dict[str, Any]]) -> tuple[list[str], dict | None, Path | None]:
    """Every reason not to send, the loaded config, and the operator root."""
    reasons: list[str] = []
    if args.send_approved_calls != len(calls) or len(calls) != PLANNED_CALLS:
        reasons.append(f"--send-approved-calls must state the plan's {PLANNED_CALLS} calls")
    if args.config is None:
        reasons.append("--config must name a config that enables the probe purpose")
        return reasons, None, None
    try:
        config = load_config(args.config)
        root = resolved_operator_root(config)
    except Exception as exc:
        reasons.append(f"the config cannot be loaded: {type(exc).__name__}")
        return reasons, None, None
    mode, cap, why = purpose_mode(config, PURPOSE)
    if mode == "off":
        reasons.append(f"the probe purpose is not enabled ({why})")
    else:
        used = calls_today(root, PURPOSE)
        if used is None:
            reasons.append("the daily call counter cannot be read")
        elif cap - used < len(calls):
            reasons.append(f"the probe daily_cap leaves {max(0, cap - used)} calls today, not {len(calls)}")
    try:
        # Read as each call reads it, so a key the sender would refuse is refused here.
        resolve_api_key(JevSettings.from_config(config["selector"]["jev"]))
    except Exception:
        reasons.append("no usable key is where selector.jev says to look")
    return reasons, config, root


def outcome_line(item: Mapping[str, Any], result) -> dict[str, object]:
    shapes = Counter(json.dumps(shape, sort_keys=True) for shape in result.shapes.values())
    return {
        "call": item["call"], "kind": item["kind"], "case": item["case"], "label": item["label"],
        "state_kind": item["state_kind"], "state_chars": len(item["state"]),
        "questions": len(item["questions"]), "types": _types(item["questions"]),
        "request_bytes": result.request_bytes, "outcome": result.outcome, "reason": result.reason,
        "mode": result.mode, "elapsed_ms": result.elapsed_ms, "usage": result.usage,
        "model_echoed": result.model_echoed,
        "answers": {name: {"type": answer.type, **answer.values()} for name, answer in result.answers.items()},
        "dropped": list(result.dropped),
        "shapes": [{"shape": json.loads(shape), "count": count} for shape, count in sorted(shapes.items())],
    }


def _emit(stream: TextIO, value: Mapping[str, object]) -> None:
    stream.write(json.dumps(value, sort_keys=True) + "\n")
    stream.flush()


def main(
    argv: list[str] | None = None, *, transport: Callable | None = None, stdout: TextIO | None = None,
    cases_path: Path = CASES,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="the config that enables the probe purpose")
    parser.add_argument(
        "--send-approved-calls", type=int, metavar="N",
        help="send, stating the approved number of calls; must equal the plan's 24",
    )
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    calls = plan(load_cases(cases_path))
    lines = [describe(item) for item in calls]
    if args.send_approved_calls is None:
        for line in lines:
            _emit(out, {"plan": line})
        _emit(out, {
            "calls": len(calls), "model": MODEL, "purpose": PURPOSE, "sent": 0,
            "request_bytes_total": sum(int(line["request_bytes"]) for line in lines),
            "to_send": f"--send-approved-calls {PLANNED_CALLS} --config PATH (probe enabled, cap free, key present)",
        })
        return 0
    reasons, config, root = refusals(args, calls)
    if reasons:
        _emit(out, {"refused": reasons, "sent": 0})
        return 2
    assert config is not None and root is not None
    sent = 0
    for item in calls:
        result = ask(
            PURPOSE, item["state"], item["questions"], config=config, root=root, transport=transport,
            model=MODEL, timeout_seconds=TIMEOUT_SECONDS,
        )
        sent += result.request_sent
        _emit(out, outcome_line(item, result))
        if result.outcome != "answered":
            _emit(out, {"stopped_at": item["call"], "sent": sent, "reason": result.reason})
            return 1
    _emit(out, {"completed": len(calls), "sent": sent})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
