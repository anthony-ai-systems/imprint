"""Once-capture marker claim, release, and retention behaviour."""

import os
import time

from imprint.capture.marks import MARK_RETENTION_DAYS, CapturedTurns, turn_identity


def test_a_turn_is_claimed_once_per_session(tmp_path):
    marks = CapturedTurns(tmp_path)
    turn = turn_identity("entry-uuid-1", "No, keep the failed source.")
    assert marks.claim("session-a", turn) is True
    assert marks.claim("session-a", turn) is False
    # A different session witnesses its own turns.
    assert marks.claim("session-b", turn) is True


def test_release_gives_the_claim_back_for_a_failed_capture(tmp_path):
    marks = CapturedTurns(tmp_path)
    turn = turn_identity(None, "No, keep the failed source.")
    assert marks.claim("session-a", turn) is True
    assert marks.release("session-a", turn) is True
    assert marks.claim("session-a", turn) is True
    # Releasing a turn that was never claimed is not an error.
    assert marks.release("session-a", "never-claimed") is True


def test_a_release_that_cannot_remove_the_mark_reports_the_stuck_claim(tmp_path):
    # Release still never raises into the hook, but a surviving mark blocks the
    # turn's retry forever, so the caller has to be able to say so.
    marks = CapturedTurns(tmp_path)
    turn = turn_identity("entry-uuid-1", "No, keep the failed source.")
    assert marks.claim("session-a", turn) is True
    stuck = next(marks.root.rglob("*.json"))
    stuck.unlink()
    stuck.mkdir()
    assert marks.release("session-a", turn) is False
    assert stuck.exists()


def test_claim_prunes_marks_past_retention_without_touching_recent_ones(tmp_path):
    marks = CapturedTurns(tmp_path)
    assert marks.claim("old-session", turn_identity("entry-old", "text")) is True
    stale = next(marks.root.rglob("*.json"))
    aged = time.time() - (MARK_RETENTION_DAYS + 1) * 86400
    os.utime(stale, (aged, aged))

    assert marks.claim("live-session", turn_identity("entry-live", "text")) is True

    assert not stale.exists()
    assert not stale.parent.exists(), "an emptied session directory is swept too"
    live = sorted(marks.root.rglob("*.json"))
    assert len(live) == 1
    # The claim that ran the prune is intact, and still holds.
    assert marks.claim("live-session", turn_identity("entry-live", "text")) is False


def test_retention_never_fails_a_capture(tmp_path):
    marks = CapturedTurns(tmp_path)
    # A runtime directory that cannot be listed must not raise into the hook.
    marks.root.parent.mkdir(parents=True, exist_ok=True)
    marks.root.write_text("not a directory")
    assert marks.prune() == 0
    marks.root.unlink()
    assert marks.claim("session-a", turn_identity("entry-uuid-1", "text")) is True
