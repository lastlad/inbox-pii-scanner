"""Tests for inbox_scanner.detection.runner.

Pins the three-way ``DetectorSet`` switch: each mode must invoke exactly
the right detectors and skip the others. Monkeypatching each detector
module's ``detect`` is the right level — it doesn't depend on the
singleton model loads.
"""

from __future__ import annotations

from inbox_scanner.detection import presidio_detector, privacy_filter_detector, runner
from inbox_scanner.detection.types import DetectorSet


def _record(sink: list[str], name: str):
    """Build a stand-in ``detect`` that appends to ``sink`` and returns []."""

    def fake(text, score_threshold=0.0):  # type: ignore[unused-argument]
        sink.append(name)
        return []

    return fake


def _boom(*_args, **_kwargs):
    """Stand-in that fails the test if it ever runs — use for the
    detector that's supposed to be skipped in the mode under test."""
    raise AssertionError("detector invoked in a mode that should skip it")


# ---------- PRESIDIO mode ----------


def test_presidio_mode_invokes_only_presidio(monkeypatch):
    """PRESIDIO mode is the default fast path — Privacy Filter must
    not run."""
    calls: list[str] = []
    monkeypatch.setattr(presidio_detector, "detect", _record(calls, "presidio"))
    monkeypatch.setattr(privacy_filter_detector, "detect", _boom)
    runner.run("SSN: 123-45-6789", detectors=DetectorSet.PRESIDIO)
    assert calls == ["presidio"]


def test_default_mode_is_presidio_only(monkeypatch):
    """No ``detectors=`` arg → PRESIDIO (the new default). Privacy
    Filter must not run."""
    calls: list[str] = []
    monkeypatch.setattr(presidio_detector, "detect", _record(calls, "presidio"))
    monkeypatch.setattr(privacy_filter_detector, "detect", _boom)
    runner.run("SSN: 123-45-6789")
    assert calls == ["presidio"]


def test_presidio_mode_still_returns_presidio_findings(monkeypatch):
    """Sanity check that the Presidio half of the runner still works
    when called through the runner (uses real Presidio, mocks PF)."""
    monkeypatch.setattr(privacy_filter_detector, "detect", _boom)
    detections = runner.run(
        "Card: 4111 1111 1111 1111",
        detectors=DetectorSet.PRESIDIO,
    )
    assert detections, "expected Presidio to find the credit card"
    assert all(d.finding.detector == "presidio" for d in detections)


# ---------- PRIVACY_FILTER mode ----------


def test_privacy_filter_mode_invokes_only_privacy_filter(monkeypatch):
    """Mirror image of PRESIDIO mode — Presidio must not run."""
    calls: list[str] = []
    monkeypatch.setattr(presidio_detector, "detect", _boom)
    monkeypatch.setattr(
        privacy_filter_detector, "detect", _record(calls, "privacy_filter")
    )
    runner.run("Some text", detectors=DetectorSet.PRIVACY_FILTER)
    assert calls == ["privacy_filter"]


# ---------- ALL mode ----------


def test_all_mode_invokes_both(monkeypatch):
    """ALL mode runs both detectors. Order matters for span-merging
    elsewhere, but the runner just concatenates — both names must
    appear in the call order Presidio→PF."""
    calls: list[str] = []
    monkeypatch.setattr(presidio_detector, "detect", _record(calls, "presidio"))
    monkeypatch.setattr(
        privacy_filter_detector, "detect", _record(calls, "privacy_filter")
    )
    runner.run("Some text", detectors=DetectorSet.ALL)
    assert calls == ["presidio", "privacy_filter"]
