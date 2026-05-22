"""Run all detectors on one piece of text and collect categorized detections."""

from __future__ import annotations

from inboxaudit.detection import (
    categorizer,
    presidio_detector,
    privacy_filter_detector,
)
from inboxaudit.detection.types import Detection, DetectorSet, Finding, Profile
from inboxaudit.logging import get_logger

log = get_logger("detection.runner")


def run(
    text: str,
    *,
    presidio_threshold: float = 0.5,
    privacy_filter_threshold: float = 0.6,
    profile: Profile = Profile.CRITICAL,
    detectors: DetectorSet = DetectorSet.PRESIDIO,
) -> list[Detection]:
    """Run the enabled detectors on ``text`` and return categorized
    detections filtered to ``profile``.

    ``detectors`` controls which detectors actually run:

    * ``PRESIDIO`` — Presidio only (the default — fast, no model load).
    * ``PRIVACY_FILTER`` — Privacy Filter only (contextual entities).
    * ``ALL`` — both.

    The ``profile`` filter is applied to whatever the enabled detectors
    produce.
    """
    findings: list[Finding] = []
    if detectors in (DetectorSet.PRESIDIO, DetectorSet.ALL):
        findings.extend(
            presidio_detector.detect(text, score_threshold=presidio_threshold)
        )
    if detectors in (DetectorSet.PRIVACY_FILTER, DetectorSet.ALL):
        findings.extend(
            privacy_filter_detector.detect(
                text, score_threshold=privacy_filter_threshold
            )
        )
    return categorizer.categorize_all(findings, profile)
