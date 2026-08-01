"""Evidence-first explicit-feedback capture."""

from .detector import FeedbackDetection, detect_explicit_feedback
from .pipeline import CapturePipeline, CaptureResult
from .provenance import SYNTHETIC_ENTRY_REASON, ProvenanceVerdict, classify_entry_provenance
from .schema import build_capture_envelope, validate_capture_envelope
from .transcript import parse_native_stop_transcript

__all__ = [
    "SYNTHETIC_ENTRY_REASON",
    "CapturePipeline",
    "CaptureResult",
    "FeedbackDetection",
    "ProvenanceVerdict",
    "build_capture_envelope",
    "classify_entry_provenance",
    "detect_explicit_feedback",
    "validate_capture_envelope",
    "parse_native_stop_transcript",
]
