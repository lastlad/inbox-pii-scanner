"""Run all detectors on one piece of text and collect categorized detections."""

from __future__ import annotations

from inbox_scanner.detection import (
    categorizer,
    presidio_detector,
    privacy_filter_detector,
)
from inbox_scanner.detection.types import Detection, Finding, Profile
from inbox_scanner.logging import get_logger

log = get_logger("detection.runner")


def run(
    text: str,
    *,
    presidio_threshold: float = 0.5,
    privacy_filter_threshold: float = 0.6,
    profile: Profile = Profile.CRITICAL,
) -> list[Detection]:
    """Run Presidio + Privacy Filter on ``text`` and return categorized
    detections filtered to ``profile``.

    Both detectors run in full — filtering happens in the categorizer.
    The cost saving from a tighter profile is therefore modest (only DB
    writes are skipped), but the signal-to-noise improvement for the
    user is significant.
    """
    findings: list[Finding] = []
    findings.extend(
        presidio_detector.detect(text, score_threshold=presidio_threshold)
    )
    findings.extend(
        privacy_filter_detector.detect(
            text, score_threshold=privacy_filter_threshold
        )
    )
    return categorizer.categorize_all(findings, profile)
