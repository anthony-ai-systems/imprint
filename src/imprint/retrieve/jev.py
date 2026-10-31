"""Optional, fail-open Jev (TypeSafe System One) relevance selection.

Nothing here runs unless an installation sets ``selector.mode`` to ``jev`` or
``shadow``. Every failure is returned as a ``fallback`` result instead of
raised, so prompt retrieval can always return to its deterministic behavior.

Egress contract for one request (``POST`` to the configured endpoint):

- ``model``: the configured model name.
- ``state``: the turn text, at most ``STATE_LIMIT`` characters.
- ``questions``: one opaque per-request name (``c0``, ``c1``, ...) per
  candidate, each ``{"type": "noul", "instructions": ...}`` quoting only the
  judgment's own statement, at most ``STATEMENT_LIMIT`` characters.

Record ids, case summaries, evidence and source receipt ids, ontology paths,
session ids, names, amounts, and metrics are never sent. Names map back to
records locally.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from imprint import __version__
from imprint.capture.detector import (
    ALWAYS_OR_NEVER,
    bullets_as_gaps,
    detect_explicit_feedback,
    feedback_sentences,
    operator_words,
    stored_words,
)
from imprint.config import SELECTOR_STATE_LIMIT, effective_selector_mode
from imprint.permissions import secure_directory, secure_file

from .engine import _parse_time
from .models import RetrievalRecord

STATE_LIMIT = SELECTOR_STATE_LIMIT
STATEMENT_LIMIT = 400
QUESTION_PREFIX = "Does this judgment apply to the current turn? Judgment: "
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_KEY_FILE_BYTES = 4096
DECISION_LOG_NAME = "jev-selector.jsonl"
DECISION_LOG_MAX_BYTES = 5 * 1024 * 1024
DECISION_LOG_TAIL_BYTES = 2 * 1024 * 1024
TRANSFER_MARKER_NAME = "jev-selector-transfer.json"
MAX_MARKER_BYTES = 4096
FALLBACK_REASONS = frozenset({
    "missing_key", "key_file_unsafe", "timeout", "http_error",
    "bad_response", "exception", "no_candidates",
})

_STOP = frozenset(
    "the a an and or of to in on for with is are was be it this that as at by from not no do does "
    "your you we they their our its if then than so can will into out up about have has had all any "
    "more most".split()
)
_URN = re.compile(r"urn:imprint:[A-Za-z0-9:._-]+")
_STANDING = re.compile(r"\b(?:from now on|going forward|from here on|always|never|whenever)\b", re.I)
# A rule is set by how a sentence is built, not by the word: "Always use X" sets
# one, and "we can always change it" sets nothing.
_RULE_SENTENCE = re.compile(
    r"\A(?:(?:thanks|also|and|great|ok|okay|so)[,!.]?\s+)?"
    r"(?:(?:from now on|going forward|from here on|whenever)\b|" + ALWAYS_OR_NEVER + r")"
    r"|\b(?:from now on|going forward|from here on)[.!]*\Z",
    re.I,
)
# These say a sentence is a rule, not what the rule is about.
_RULE_WORDS = frozenset({"always", "alway", "never", "whenever", "from", "now", "going", "forward", "here"})
_STANDING_READ_CHARS = 1000
_FEEDBACK_READ_CHARS = 8000
# Reading records for rules and for staleness shares a turn's time with the request,
# so each stops at this part of the time left when it starts.
SCAN_SHARE = 1 / 3
# The only route where a record had to read as feedback to get in.
_DETECTED_MECHANISM = "claude_code_stop_hook"
_KEY = re.compile(r"[\x21-\x7e]{1,4096}")
_ECHOED_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


# Mode resolution lives in config so deterministic hooks never import this module.
effective_mode = effective_selector_mode


@dataclass(frozen=True)
class JevSettings:
    endpoint: str
    model: str
    api_key_env: str = "TYPESAFE_API_KEY"
    api_key_file: str | None = None
    top: int = 20
    score_floor: float = 0.55
    timeout_seconds: float = 3.0
    prefilter_keep: int = 150
    standing_floor: float = 0.25
    standing_rare_share: float = 0.15
    standing_slots: int = 2

    @classmethod
    def from_config(cls, jev: Mapping[str, Any]) -> "JevSettings":
        return cls(
            endpoint=str(jev["endpoint"]),
            model=str(jev["model"]),
            api_key_env=str(jev["api_key_env"]),
            api_key_file=jev["api_key_file"],
            top=int(jev["top"]),
            score_floor=float(jev["score_floor"]),
            timeout_seconds=float(jev["timeout_seconds"]),
            prefilter_keep=int(jev["prefilter_keep"]),
            standing_floor=float(jev["standing_rules"]["score_floor"]),
            standing_rare_share=float(jev["standing_rules"]["rare_word_share"]),
            standing_slots=int(jev["standing_rules"]["slots"]),
        )


@dataclass(frozen=True)
class SelectionResult:
    """One selector call. ``outcome`` is picked, no_picks, or fallback."""

    picks: tuple[tuple[RetrievalRecord, float], ...] = ()
    outcome: str = "fallback"
    fallback_reason: str | None = None
    elapsed_ms: int = 0
    model_echoed: str | None = None
    usage: dict[str, int] | None = None
    candidates_sent: int = 0
    # True once request bytes were handed to the transport, whatever followed.
    request_sent: bool = False
    # Size of that request. The context budget bounds delivery, not this.
    request_bytes: int = 0
    # Standing rules among ``picks``, which lead them.
    reserved: frozenset[str] = frozenset()
    # Stale records passed over on the way to the request.
    withheld: int = 0
    # The stale check ran out of time, so the request holds fewer records.
    scan_cut: bool = False


class _KeyUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _BadResponse(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


@lru_cache(maxsize=None)
def _spoken(statement: str) -> str:
    return stored_words(operator_words(statement))


def forget_statements() -> None:
    """Start a turn's reading afresh, which bounds the cache by the pool."""
    _spoken.cache_clear()


def _words(record: RetrievalRecord) -> str:
    value = record.judgment_statement
    return _spoken(value) if isinstance(value, str) else ""


def egress_text(record: RetrievalRecord) -> str | None:
    """The only record content a remote selector may receive.

    Built from ``judgment_statement`` (never the retrieval ``text`` fallback
    chain), keeping only the operator's words, whitespace-collapsed, with Imprint
    URNs replaced, and truncated. A record stored by an older build can open with
    host text, which would otherwise fill the whole statement.
    """
    words = _words(record)
    text = " ".join(_URN.sub("[ref]", words).split())[:STATEMENT_LIMIT]
    return text or None


def reads_as_feedback(record: RetrievalRecord) -> bool:
    """Whether a captured message still reads as feedback under today's rules.

    Each record judged was a reply, so the rules for replies apply. A record with
    no words has nothing to judge. A hook runs this per prompt, so it reads as far
    as moves the answer for almost every record.
    """
    words = _words(record)[:_FEEDBACK_READ_CHARS]
    if not words.strip():
        return True
    return detect_explicit_feedback(words, prior_assistant_output="-").is_feedback or bool(feedback_sentences(words))


@dataclass(frozen=True)
class StaleCheck:
    """Which records must still read as feedback, given when the rules last changed.

    Older builds kept messages the rules have since stopped matching. The record
    stays in the store. Only stop-hook captures are judged: ratified knowledge, an
    explicit capture and an import never met the detector. A capture since the
    change met today's rules as it was made, and what is stored has lost the quote
    those rules read around, so judging it again could only be wrong.
    """

    changed: datetime

    @classmethod
    def since(cls, before: str) -> "StaleCheck":
        return cls(_parse_time(before))

    def applies_to(self, record: RetrievalRecord) -> bool:
        """Whether a record has to be read at all, which takes no reading to answer."""
        return (
            record.authority_tier == "captured_judgment"
            and record.capture_mechanism == _DETECTED_MECHANISM
            and _parse_time(record.valid_from) < self.changed
        )

    def keeps(self, record: RetrievalRecord) -> bool:
        return not self.applies_to(record) or reads_as_feedback(record)


def rule_sentences(record: RetrievalRecord) -> list[str]:
    """Return the sentences that set a rule for later work, where Jev can read them."""
    words = _words(record)
    if not _STANDING.search(words, 0, STATEMENT_LIMIT):
        return []
    return [
        sentence for sentence in feedback_sentences(bullets_as_gaps(words), within=_STANDING_READ_CHARS, any_length=True)
        if _RULE_SENTENCE.search(sentence) and not sentence.endswith("?") and words.find(sentence) < STATEMENT_LIMIT
    ]


def newest_rules(
    entries: Sequence[RetrievalRecord], keep: int, stale: StaleCheck | None, stop: Callable[[], bool],
    said: frozenset[str | None] = frozenset(),
) -> frozenset[str]:
    """Return the newest ``keep`` records that set different rules, as far as ``stop`` allows.

    A rule said twice is two records, and would otherwise take a place from another
    rule. ``said`` holds statements this session already has.
    """
    found: list[str] = []
    seen = set(said)
    for item in sorted(entries, key=lambda item: _parse_time(item.valid_from), reverse=True):
        if len(found) >= keep or stop():
            break
        if egress_text(item) not in seen and rule_sentences(item) and (stale is None or stale.keeps(item)):
            seen.add(egress_text(item))
            found.append(item.record_id)
    return frozenset(found)


def _folded(value: str) -> set[str]:
    return {word[:-1] if len(word) > 3 and word.endswith("s") else word for word in _tokens(value)}


def rare_matches(
    turn_text: str, entries: Sequence[RetrievalRecord], standing: frozenset[str], share: float,
) -> frozenset[str]:
    """Return the standing rules whose own sentence shares with the turn a word few records use.

    Jev's score alone does not separate a rule that applies from one that does
    not, and a word most records use says nothing either way.
    """
    query = _folded(turn_text) - _RULE_WORDS
    counts: dict[str, int] = {}
    # Saying a rule again must not cost it its rare word.
    texts = {egress_text(item) or "" for item in entries}
    for text in texts:
        for word in _folded(text) & query:
            counts[word] = counts.get(word, 0) + 1
    # A word only the rule itself uses is rare in a pool of any size.
    limit = max(1.0, share * len(texts))
    return frozenset(
        item.record_id for item in entries
        if item.record_id in standing
        and any(counts.get(word, 0) <= limit for word in _folded(" ".join(rule_sentences(item))) & query)
    )


def _tokens(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z][a-z'\-]{2,}", value.casefold()) if word not in _STOP}


def prefilter(
    turn_text: str, entries: Sequence[RetrievalRecord], keep: int = 150,
    *, standing: frozenset[str] = frozenset(),
    accept: Callable[[RetrievalRecord], bool] | None = None,
) -> list[RetrievalRecord]:
    """Keep the IDF-weighted lexical best ``keep`` candidates before egress.

    Ties (including turns with no lexical overlap) prefer newer records, then
    record id bytes, so the kept set is deterministic. ``standing`` records are
    kept whatever their overlap, in place of the weakest. ``accept`` can pass over
    a record, and is asked only of those that would otherwise be kept.
    """
    if keep <= 0:
        return []
    query = _tokens(turn_text)
    documents = [_tokens(egress_text(item) or "") for item in entries]
    frequency: dict[str, int] = {}
    for document in documents:
        for token in document & query:
            frequency[token] = frequency.get(token, 0) + 1
    count = max(1, len(entries))
    ranked = []
    for item, document in zip(entries, documents):
        score = sum(math.log1p(count / frequency[token]) for token in document & query)
        score /= math.sqrt(len(document) + 5)
        ranked.append((-score, -_parse_time(item.valid_from).timestamp(), item.record_id.encode("utf-8"), item))
    ranked.sort(key=lambda value: value[:3])
    rules = [value[3] for value in ranked if value[3].record_id in standing][:keep]
    others: list[RetrievalRecord] = []
    for *_, item in ranked:
        if len(others) >= keep - len(rules):
            break
        if item.record_id not in standing and (accept is None or accept(item)):
            others.append(item)
    return others + rules


def _read_key_file(path: Path) -> str:
    """Read a private key file bound to one descriptor; never echo it."""
    if not path.is_absolute():
        raise _KeyUnavailable("key_file_unsafe")
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        raise _KeyUnavailable("missing_key") from exc
    except OSError as exc:
        raise _KeyUnavailable("key_file_unsafe") from exc
    if not stat.S_ISREG(before.st_mode):
        raise _KeyUnavailable("key_file_unsafe")
    if os.name != "nt" and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077):
        raise _KeyUnavailable("key_file_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _KeyUnavailable("key_file_unsafe") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise _KeyUnavailable("key_file_unsafe")
        data = os.read(descriptor, MAX_KEY_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_KEY_FILE_BYTES:
        raise _KeyUnavailable("key_file_unsafe")
    try:
        key = data.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise _KeyUnavailable("missing_key") from exc
    if _KEY.fullmatch(key) is None:
        raise _KeyUnavailable("missing_key")
    return key


def resolve_api_key(settings: JevSettings) -> str:
    """Return the bearer key; the environment variable wins over the file."""
    value = os.environ.get(settings.api_key_env, "").strip()
    if value:
        if _KEY.fullmatch(value) is None:
            raise _KeyUnavailable("missing_key")
        return value
    if settings.api_key_file is None:
        raise _KeyUnavailable("missing_key")
    return _read_key_file(Path(settings.api_key_file).expanduser())


def api_key_present(settings: JevSettings) -> bool:
    """Content-free presence check for health; never reads the key file."""
    if os.environ.get(settings.api_key_env, "").strip():
        return True
    if settings.api_key_file is None:
        return False
    path = Path(settings.api_key_file).expanduser()
    try:
        status = os.lstat(path)
    except OSError:
        return False
    if not path.is_absolute() or not stat.S_ISREG(status.st_mode) or status.st_size <= 0:
        return False
    if os.name != "nt" and (status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) & 0o077):
        return False
    return True


def _opener():
    """An opener that refuses redirects: following one would re-send the
    bearer key to another URL, so a 3xx surfaces as an HTTP error instead."""
    import urllib.request  # deferred: only a live selector request needs it

    class RefuseRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(RefuseRedirect)


def http_post(endpoint: str, body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    """POST once over HTTPS without redirects and return a bounded body."""
    import urllib.request

    opener = _opener()
    request = urllib.request.Request(endpoint, data=body, headers=dict(headers), method="POST")
    with opener.open(request, timeout=timeout) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise _BadResponse("response exceeds the size bound")
    return data


def _failure_reason(exc: BaseException) -> str:
    """Map a transport failure onto the closed fallback vocabulary."""
    import http.client
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return "http_error"
    if isinstance(exc, urllib.error.URLError):
        return "timeout" if isinstance(exc.reason, TimeoutError) else "http_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return "http_error"
    return "exception"


def _run_with_deadline(function: Callable[[], bytes], timeout: float) -> bytes:
    """Run ``function`` in a daemon thread and stop waiting at ``timeout``.

    An abandoned request thread cannot keep the hook process alive.
    """
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = function()
        except BaseException as exc:  # handed back to the caller's thread
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="imprint-jev-request", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout))
    if worker.is_alive():
        raise _DeadlineExceeded
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _score(answer: Any) -> float | None:
    """Accept only the documented ``{"type": "noul", "noul": p}`` shape."""
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        return None
    value = answer.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):  # an integer too large for a float
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return value


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage = {}
    for field in ("input_tokens", "output_tokens"):
        count = value.get(field)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            usage[field] = count
    return usage or None


def _echoed_model(value: Any) -> str | None:
    return value if isinstance(value, str) and _ECHOED_MODEL.fullmatch(value) else None


def request_body(model: str, state: str, candidates: Sequence[RetrievalRecord]) -> tuple[dict[str, Any], dict[str, RetrievalRecord]]:
    """Return the exact request body and the local name-to-record map."""
    names: dict[str, RetrievalRecord] = {}
    questions: dict[str, dict[str, str]] = {}
    for index, item in enumerate(candidates):
        text = egress_text(item)
        if text is None:
            continue
        name = f"c{index}"
        names[name] = item
        questions[name] = {"type": "noul", "instructions": QUESTION_PREFIX + text}
    return {"model": model, "state": state[:STATE_LIMIT], "questions": questions}, names


class JevSelector:
    """Score prefiltered judgments against the turn with one bounded request."""

    def __init__(
        self,
        settings: JevSettings,
        *,
        transport: Callable[[str, bytes, Mapping[str, str], float], bytes] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._clock = clock

    def _result(self, started: float, **values: Any) -> SelectionResult:
        elapsed = max(0, int(round((self._clock() - started) * 1000)))
        return SelectionResult(elapsed_ms=elapsed, **values)

    def select(
        self, state: str, entries: Sequence[RetrievalRecord], *, standing: frozenset[str] = frozenset(),
        stale: StaleCheck | None = None, prompt: str | None = None,
    ) -> SelectionResult:
        """Return picks or a fallback reason. Never raises.

        ``timeout_seconds`` is a total wall-clock deadline for key resolution,
        prefiltering, the request, and parsing. The best ``standing`` records at
        or above their own floor that share a rare word with ``prompt``, or with
        the turn when it is only the prompt, lead the picks, so the budget reaches
        them first. ``stale`` keeps records out of the request, and a record it has
        not read in its share of the deadline stays out too.
        """
        started = self._clock()
        deadline = started + self.settings.timeout_seconds
        scan_ends: float | None = None
        sent = 0
        request_bytes = 0
        request_sent = False
        withheld = 0
        scan_cut = False

        def passed(item: RetrievalRecord) -> bool:
            nonlocal scan_ends, scan_cut, withheld
            if stale is None or not stale.applies_to(item):
                return True
            now = self._clock()
            if scan_ends is None:
                scan_ends = now + (deadline - now) * SCAN_SHARE
            if now >= scan_ends:
                scan_cut = True
                return False
            fresh = reads_as_feedback(item)
            withheld += not fresh
            return fresh

        try:
            try:
                key = resolve_api_key(self.settings)
            except _KeyUnavailable as exc:
                return self._result(started, fallback_reason=exc.reason)
            if entries and self._clock() >= deadline:
                return self._result(started, fallback_reason="timeout")
            sendable = [item for item in entries if egress_text(item)]
            candidates = prefilter(state, sendable, self.settings.prefilter_keep, standing=standing, accept=passed)
            if not candidates and scan_cut:
                return self._result(started, fallback_reason="timeout", withheld=withheld, scan_cut=True)
            if not candidates and withheld:
                return self._result(started, outcome="no_picks", withheld=withheld)
            if not candidates:
                return self._result(started, fallback_reason="no_candidates")
            body, names = request_body(self.settings.model, state, candidates)
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": f"imprint-local/{__version__}",
            }
            remaining = deadline - self._clock()
            if remaining <= 0:
                return self._result(started, fallback_reason="timeout", withheld=withheld, scan_cut=scan_cut)
            transport = self._transport or http_post
            sent = len(names)
            request_bytes = len(encoded)
            request_sent = True
            raw = _run_with_deadline(
                lambda: transport(self.settings.endpoint, encoded, headers, remaining), remaining,
            )
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise _BadResponse("response is not JSON") from exc
            if not isinstance(value, dict):
                raise _BadResponse("response must be an object")
            answers = value.get("answers")
            if not isinstance(answers, dict) or not answers:
                raise _BadResponse("response answers must be a non-empty object")
            scored: list[tuple[RetrievalRecord, float]] = []
            valid = 0
            for name, answer in answers.items():
                record = names.get(name) if isinstance(name, str) else None
                score = _score(answer) if record is not None else None
                if score is None:
                    continue  # unknown names and malformed answers are dropped
                valid += 1
                scored.append((record, score))
            if not valid:
                raise _BadResponse("response carries no valid answer for a sent question")
            scored.sort(key=lambda item: (-item[1], item[0].record_id.encode("utf-8")))
            matched = frozenset()
            if standing:
                said = _spoken(state if prompt is None else prompt)
                matched = rare_matches(said, sendable, standing, self.settings.standing_rare_share)
            reserved = [
                pick for pick in scored
                if pick[0].record_id in matched and pick[1] >= self.settings.standing_floor
            ][: self.settings.standing_slots]
            held = {pick[0].record_id for pick in reserved}
            picks = tuple((reserved + [
                pick for pick in scored if pick[1] >= self.settings.score_floor and pick[0].record_id not in held
            ])[: self.settings.top])
            return self._result(
                started,
                picks=picks,
                reserved=frozenset(pick[0].record_id for pick in picks) & held,
                withheld=withheld,
                scan_cut=scan_cut,
                outcome="picked" if picks else "no_picks",
                model_echoed=_echoed_model(value.get("model")),
                usage=_usage(value.get("usage")),
                candidates_sent=sent,
                request_sent=True,
                request_bytes=request_bytes,
            )
        except _DeadlineExceeded:
            reason = "timeout"
        except _BadResponse:
            reason = "bad_response"
        except Exception as exc:
            reason = _failure_reason(exc)
        return self._result(
            started, fallback_reason=reason, candidates_sent=sent, request_sent=request_sent,
            request_bytes=request_bytes, withheld=withheld, scan_cut=scan_cut,
        )


def _utc_stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def decision_entry(
    result: SelectionResult, *, mode: str, session_ref: str, receipt_scope: str | None,
    stages: Mapping[str, int] | None = None, delivery_outcome: str | None = None,
) -> dict[str, object]:
    """A content-free decision record: no prompt, judgment text, or key.

    ``outcome`` is Jev's result; ``delivery_outcome`` and ``stages`` are what became
    of it in this turn, and are null on a shadow or fallback line, which delivers
    nothing of Jev's. The last three fields appear only when they have something
    to say, so a line with both settings off is unchanged.
    """
    return {
        "candidates_sent": result.candidates_sent,
        "delivery_outcome": delivery_outcome,
        "elapsed_ms": result.elapsed_ms,
        "fallback_reason": result.fallback_reason,
        "mode": mode,
        "model_echoed": result.model_echoed,
        "outcome": result.outcome,
        "picks": [{"record_id": record.record_id, "score": round(score, 4)} for record, score in result.picks],
        "receipt_scope": receipt_scope,
        "request_bytes": result.request_bytes,
        "request_sent": result.request_sent,
        "session": session_ref,
        "stages": dict(stages) if stages is not None else None,
        "ts": _utc_stamp(),
        "usage": result.usage,
        **({"reserved_picks": len(result.reserved)} if result.reserved else {}),
        **({"stale_withheld": result.withheld} if result.withheld else {}),
        **({"stale_scan_cut": True} if result.scan_cut else {}),
    }


def decision_log_path(root: Path) -> Path:
    return Path(root) / "logs" / DECISION_LOG_NAME


def log_decision(root: Path, entry: Mapping[str, object]) -> None:
    """Append one line, rotating once at the size cap. Never raises."""
    try:
        path = decision_log_path(root)
        line = (json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        if not path.parent.is_dir():
            secure_directory(path.parent)
        try:
            size = path.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            size = -1
        if size >= 0 and size + len(line) > DECISION_LOG_MAX_BYTES:
            os.replace(path, path.with_name(DECISION_LOG_NAME + ".1"))
            size = -1
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
        if size < 0:
            secure_file(path)
    except Exception:
        pass


def transfer_marker_path(root: Path) -> Path:
    return Path(root) / "logs" / TRANSFER_MARKER_NAME


def _read_marker(path: Path) -> dict[str, str]:
    """The marker's timestamps; anything unreadable or unexpected is absent."""
    try:
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_MARKER_BYTES:
            return {}
        with path.open("rb") as handle:
            value = json.loads(handle.read(MAX_MARKER_BYTES + 1))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key] for key in ("last_success_ts", "last_transfer_ts")
        if isinstance(value.get(key), str) and _STAMP.fullmatch(value[key])
    }


def record_transfer(root: Path, *, success: bool) -> None:
    """Note that request bytes left the machine. Never raises.

    Written whatever ``log_decisions`` says, and never rotated, so health can
    report transfers truthfully. It holds two timestamps and nothing else.
    """
    try:
        path = transfer_marker_path(root)
        if not path.parent.is_dir():
            secure_directory(path.parent)
        stamp = _utc_stamp()
        value = {
            "last_success_ts": stamp if success else _read_marker(path).get("last_success_ts"),
            "last_transfer_ts": stamp,
        }
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".imprint-jev-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            secure_file(temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except Exception:
        pass


def _tail_lines(path: Path, limit: int) -> tuple[list[bytes], int]:
    """Complete lines from the last ``limit`` bytes of one log file, and the
    number of bytes read. A missing or irregular file has none."""
    if limit <= 0:
        return [], 0
    try:
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode):
            return [], 0
        with path.open("rb") as handle:
            offset = max(0, status.st_size - limit)
            handle.seek(offset)
            data = handle.read(limit)
    except OSError:
        return [], 0
    lines = data.split(b"\n")
    if offset:
        lines = lines[1:]  # the first line of a tail read may be partial
    return lines, len(data)


def decision_log_summary(root: Path, *, now: datetime | None = None) -> dict[str, object]:
    """Content-free counts from the newest ``DECISION_LOG_TAIL_BYTES`` of the
    decision log, continuing into the rotated file when the current one is
    shorter, so a rotation cannot hide recent lines."""
    current = now or datetime.now(timezone.utc)
    summary: dict[str, object] = {
        "last_success_ts": None, "last_transfer_ts": None, "fallbacks_24h": 0,
    }
    path = decision_log_path(root)
    lines, used = _tail_lines(path, DECISION_LOG_TAIL_BYTES)
    rotated, _ = _tail_lines(path.with_name(DECISION_LOG_NAME + ".1"), DECISION_LOG_TAIL_BYTES - used)
    cutoff = current - timedelta(hours=24)
    fallbacks = 0
    for raw in rotated + lines:
        try:
            entry = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("ts"), str):
            continue
        stamp = entry["ts"]
        if entry.get("request_sent") is True:
            summary["last_transfer_ts"] = max(stamp, summary["last_transfer_ts"] or stamp)
        if entry.get("outcome") in {"picked", "no_picks"}:
            summary["last_success_ts"] = max(stamp, summary["last_success_ts"] or stamp)
        elif entry.get("outcome") == "fallback" and _parse_time(stamp) >= cutoff:
            fallbacks += 1
    summary["fallbacks_24h"] = fallbacks
    return summary


def selector_health(root: Path, selector: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, object]:
    """Truthful, content-free selector state for the health report."""
    current = now or datetime.now(timezone.utc)
    mode = effective_mode(selector)
    jev = selector.get("jev") if isinstance(selector, Mapping) else None
    host = ""
    key_present = False
    if isinstance(jev, Mapping):
        try:
            host = urlsplit(str(jev.get("endpoint", ""))).hostname or ""
            key_present = api_key_present(JevSettings.from_config(jev))
        except (KeyError, TypeError, ValueError):
            pass
    value: dict[str, object] = {
        "mode": mode,
        "key_present": key_present,
        "endpoint_host": host,
        "network_state": "offline",
        "last_transfer_age_seconds": -1,
        "last_success_ts": "",
        "fallbacks_24h": 0,
    }
    if mode == "deterministic":
        return value
    summary = decision_log_summary(root, now=current)
    # The marker covers transfers the decision log cannot show: logging
    # disabled, or lines rotated away.
    marker = _read_marker(transfer_marker_path(root))

    def latest(name: str) -> str | None:
        stamps = [item for item in (summary[name], marker.get(name)) if isinstance(item, str)]
        return max(stamps) if stamps else None

    last_transfer = latest("last_transfer_ts")
    value["network_state"] = "transferred" if last_transfer else "idle"
    if last_transfer is not None:
        age = int((current - _parse_time(last_transfer)).total_seconds())
        value["last_transfer_age_seconds"] = max(0, age)
    value["last_success_ts"] = latest("last_success_ts") or ""
    value["fallbacks_24h"] = summary["fallbacks_24h"]
    return value


__all__ = [
    "DECISION_LOG_NAME",
    "FALLBACK_REASONS",
    "JevSelector",
    "JevSettings",
    "QUESTION_PREFIX",
    "SCAN_SHARE",
    "STATEMENT_LIMIT",
    "STATE_LIMIT",
    "SelectionResult",
    "StaleCheck",
    "api_key_present",
    "decision_entry",
    "decision_log_summary",
    "effective_mode",
    "egress_text",
    "forget_statements",
    "http_post",
    "log_decision",
    "newest_rules",
    "prefilter",
    "rare_matches",
    "reads_as_feedback",
    "rule_sentences",
    "record_transfer",
    "request_body",
    "resolve_api_key",
    "selector_health",
    "transfer_marker_path",
]
