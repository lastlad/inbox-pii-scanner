"""Run all detectors on one piece of text and collect categorized detections."""

from __future__ import annotations

from inbox_scanner.detection import (
    categorizer,
    presidio_detector,
    privacy_filter_detector,
)
from inbox_scanner.detection.types import Detection, DetectorSet, Finding, Profile
from inbox_scanner.logging import get_logger

log = get_logger("detection.runner")


def run(
    text: str,
    *,
    presidio_threshold: float = 0.5,
    privacy_filter_threshold: float = 0.6,
    profile: Profile = Profile.CRITICAL,
    detectors: DetectorSet = DetectorSet.ALL,
) -> list[Detection]:
    """Run the enabled detectors on ``text`` and return categorized
    detections filtered to ``profile``.

    ``detectors`` controls which detectors actually run. ``ALL`` runs
    Presidio + Privacy Filter (the default). ``PRESIDIO`` runs Presidio
    only — drops the slow contextual detector for low-compute hosts.
    The ``profile`` filter is applied to whatever the enabled detectors
    produce.
    """
    findings: list[Finding] = []
    findings.extend(
        presidio_detector.detect(text, score_threshold=presidio_threshold)
    )
    if detectors == DetectorSet.ALL:
        findings.extend(
            privacy_filter_detector.detect(
                text, score_threshold=privacy_filter_threshold
            )
        )
    return categorizer.categorize_all(findings, profile)
