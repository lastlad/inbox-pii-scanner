"""Run all detectors on one piece of text and collect categorized detections."""

from __future__ import annotations

from inbox_scanner.detection import (
    categorizer,
    custom_regex,
    presidio_detector,
    privacy_filter_detector,
)
from inbox_scanner.detection.types import Detection, Finding
from inbox_scanner.logging import get_logger

log = get_logger("detection.runner")


def run(
    text: str,
    *,
    presidio_threshold: float = 0.5,
    privacy_filter_threshold: float = 0.6,
    skip_privacy_filter: bool = False,
) -> list[Detection]:
    """Run Presidio + Privacy Filter + custom regex on ``text`` and return
    categorized detections.

    ``skip_privacy_filter`` is provided as a v1 escape hatch — the model
    is the slow part of the pipeline and turning it off lets us iterate
    faster on Presidio + regex tuning.
    """
    findings: list[Finding] = []

    findings.extend(
        presidio_detector.detect(text, score_threshold=presidio_threshold)
    )
    if not skip_privacy_filter:
        findings.extend(
            privacy_filter_detector.detect(
                text, score_threshold=privacy_filter_threshold
            )
        )
    findings.extend(custom_regex.detect(text))

    return categorizer.categorize_all(findings)
