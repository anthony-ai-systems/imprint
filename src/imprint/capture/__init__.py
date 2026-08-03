"""Evidence-first explicit-feedback capture."""

from .detector import FeedbackDetection, detect_explicit_feedback
from .marks import ALREADY_CAPTURED_REASON, CapturedTurns, turn_identity
from .pipeline import CapturePipeline, CaptureResult
from .provenance import (
    NO_OPERATOR_MESSAGE_REASON,
    SYNTHETIC_ENTRY_REASON,
    ProvenanceVerdict,
    classify_entry_provenance,
)
from .schema import build_capture_envelope, validate_capture_envelope
from .transcript import parse_native_stop_transcript

__all__ = [
    "ALREADY_CAPTURED_REASON",
    "NO_OPERATOR_MESSAGE_REASON",
    "SYNTHETIC_ENTRY_REASON",
    "CapturePipeline",
    "CaptureResult",
    "CapturedTurns",
    "FeedbackDetection",
    "ProvenanceVerdict",
    "build_capture_envelope",
    "classify_entry_provenance",
    "detect_explicit_feedback",
    "turn_identity",
    "validate_capture_envelope",
    "parse_native_stop_transcript",
]
