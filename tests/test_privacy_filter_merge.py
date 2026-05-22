"""Tests for the BIE→S coalescing pass in privacy_filter_detector.

The merger is a pure function — no model load needed.
"""

from __future__ import annotations

from inboxaudit.detection.privacy_filter_detector import (
    _merge_adjacent_same_subtype,
)
from inboxaudit.detection.types import Finding


def _f(subtype: str, start: int, end: int, conf: float = 0.9) -> Finding:
    return Finding(
        detector="privacy_filter",
        subtype=subtype,
        span_text="x" * (end - start),
        span_start=start,
        span_end=end,
        confidence=conf,
    )


def test_empty_input():
    assert _merge_adjacent_same_subtype([], "anything") == []


def test_single_finding_passthrough():
    f = _f("private_person", 5, 10)
    out = _merge_adjacent_same_subtype([f], "abcdefghijklmnop")
    assert out == [f]


def test_merges_touching_same_subtype():
    """The canonical BIE→S case: ``"Sa…V"`` (5-12) + ``"emu"`` (12-15) →
    one private_person at 5-15."""
    text = "0123 Santosh Vemu wrote"
    out = _merge_adjacent_same_subtype(
        [_f("private_person", 5, 12), _f("private_person", 12, 15)],
        text,
    )
    assert len(out) == 1
    assert out[0].span_start == 5
    assert out[0].span_end == 15
    assert out[0].span_text == text[5:15]
    assert out[0].subtype == "private_person"


def test_merges_with_one_char_gap():
    """Gap of exactly one character (e.g. an unlabelled space) still
    merges — that's the threshold the function documents."""
    text = "First Last says hello"
    out = _merge_adjacent_same_subtype(
        [_f("private_person", 0, 5), _f("private_person", 6, 10)],
        text,
    )
    assert len(out) == 1
    assert out[0].span_start == 0
    assert out[0].span_end == 10


def test_does_not_merge_with_larger_gap():
    """Two unrelated person mentions far apart stay separate."""
    text = "John was here. Mary was there."
    out = _merge_adjacent_same_subtype(
        [_f("private_person", 0, 4), _f("private_person", 15, 19)],
        text,
    )
    assert len(out) == 2


def test_does_not_merge_different_subtypes():
    out = _merge_adjacent_same_subtype(
        [_f("private_person", 0, 5), _f("private_address", 5, 15)],
        "John 123 Main St.",
    )
    assert len(out) == 2
    assert {f.subtype for f in out} == {"private_person", "private_address"}


def test_does_not_merge_different_detectors():
    """Cross-detector merging would lose the independent confirmation
    signal — and in practice subtype namespaces differ anyway, but pin
    it as policy."""
    a = Finding(
        detector="presidio",
        subtype="X",
        span_text="ab",
        span_start=0,
        span_end=2,
        confidence=0.9,
    )
    b = Finding(
        detector="privacy_filter",
        subtype="X",
        span_text="cd",
        span_start=2,
        span_end=4,
        confidence=0.9,
    )
    assert len(_merge_adjacent_same_subtype([a, b], "abcd")) == 2


def test_overlapping_findings_merge_into_union():
    text = "0123456789abcdef"
    out = _merge_adjacent_same_subtype(
        [_f("account_number", 0, 7), _f("account_number", 5, 10)],
        text,
    )
    assert len(out) == 1
    assert out[0].span_start == 0
    assert out[0].span_end == 10


def test_merged_confidence_is_length_weighted():
    text = "x" * 20
    out = _merge_adjacent_same_subtype(
        [
            _f("private_person", 0, 8, conf=1.0),  # 8 chars at 1.0
            _f("private_person", 8, 10, conf=0.6),  # 2 chars at 0.6
        ],
        text,
    )
    assert len(out) == 1
    # Length-weighted: (1.0*8 + 0.6*2) / 10 = 0.92
    assert abs(out[0].confidence - 0.92) < 1e-9


def test_three_way_merge():
    """A single entity shredded into three tokens still collapses to one."""
    out = _merge_adjacent_same_subtype(
        [
            _f("private_address", 10, 20),
            _f("private_address", 20, 25),
            _f("private_address", 25, 30),
        ],
        "x" * 40,
    )
    assert len(out) == 1
    assert out[0].span_start == 10
    assert out[0].span_end == 30


def test_merge_is_order_independent():
    """Inputs may arrive in any order — sorting is the merger's job."""
    text = "x" * 30
    findings = [
        _f("private_person", 20, 25),
        _f("private_person", 5, 10),
        _f("private_person", 10, 15),  # touches the second
    ]
    out = _merge_adjacent_same_subtype(findings, text)
    assert len(out) == 2
    out_by_start = sorted(out, key=lambda f: f.span_start)
    assert out_by_start[0].span_start == 5
    assert out_by_start[0].span_end == 15
    assert out_by_start[1].span_start == 20
