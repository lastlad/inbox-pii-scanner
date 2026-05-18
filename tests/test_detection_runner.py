"""Tests for inbox_scanner.detection.runner.

Specifically pins the ``detectors=DetectorSet.PRESIDIO`` fast-path: we
must not invoke the heavy Privacy Filter pipeline when the caller opts
into Presidio-only mode. Monkeypatching the privacy-filter module's
``detect`` is the right level — it doesn't depend on the singleton
model load.
"""

from __future__ import annotations

import pytest

from inbox_scanner.detection import privacy_filter_detector, runner
from inbox_scanner.detection.types import DetectorSet, Finding


def _boom(*_args, **_kwargs):
    """Stand-in for privacy_filter_detector.detect that fails the test
    if it ever runs."""
    raise AssertionError(
        "privacy_filter_detector.detect was called in Presidio-only mode"
    )


def test_presidio_only_does_not_invoke_privacy_filter(monkeypatch):
    """In ``DetectorSet.PRESIDIO`` mode the Privacy Filter detector must
    not be called — that's the whole point of the fast path."""
    monkeypatch.setattr(privacy_filter_detector, "detect", _boom)
    # A short text with an SSN-shaped value so Presidio has something to
    # find; we don't actually care about the return value, only that the
    # call completes without raising.
    runner.run(
        "SSN: 123-45-6789",
        detectors=DetectorSet.PRESIDIO,
    )


def test_default_mode_invokes_privacy_filter(monkeypatch):
    """And the inverse: the default ``DetectorSet.ALL`` must keep
    calling Privacy Filter so we don't accidentally regress to fast
    mode on every scan."""
    called: list[str] = []

    def _record(text, score_threshold=0.6):  # type: ignore[unused-argument]
        called.append(text)
        return []  # no findings — Privacy Filter is mocked out

    monkeypatch.setattr(privacy_filter_detector, "detect", _record)
    runner.run("SSN: 123-45-6789")  # default detectors=ALL
    assert called, "expected privacy_filter_detector.detect to be invoked"


def test_presidio_only_still_returns_presidio_findings(monkeypatch):
    """Sanity check that the Presidio half of the runner still works
    when Privacy Filter is skipped."""
    monkeypatch.setattr(privacy_filter_detector, "detect", _boom)
    detections = runner.run(
        "Card: 4111 1111 1111 1111",
        detectors=DetectorSet.PRESIDIO,
    )
    # Presidio recognises CREDIT_CARD via Luhn; expect at least one
    # categorized detection back, all under the presidio detector.
    assert detections, "expected Presidio to find the credit card"
    assert all(d.finding.detector == "presidio" for d in detections)
