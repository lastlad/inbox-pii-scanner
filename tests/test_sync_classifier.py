"""Tests for the attachment skip-filter in inbox_scanner.gmail.sync."""

from __future__ import annotations

from inbox_scanner.gmail.sync import (
    ATT_PENDING,
    ATT_SKIPPED_FILTER,
    ATT_SKIPPED_TOO_LARGE,
    SKIP_MIME_TYPES,
    _classify_attachment,
)

MAX = 50 * 1024 * 1024


def test_normal_pdf_pending():
    assert _classify_attachment("application/pdf", 200_000, MAX) == ATT_PENDING


def test_skipped_mime():
    for mime in SKIP_MIME_TYPES:
        assert _classify_attachment(mime, 100_000, MAX) == ATT_SKIPPED_FILTER


def test_too_small_is_skipped():
    assert _classify_attachment("image/png", 500, MAX) == ATT_SKIPPED_FILTER


def test_too_large_is_skipped():
    assert _classify_attachment("application/pdf", MAX + 1, MAX) == ATT_SKIPPED_TOO_LARGE


def test_size_threshold_inclusive():
    # MIN_ATTACHMENT_SIZE = 1024; exactly 1024 must pass.
    assert _classify_attachment("application/pdf", 1024, MAX) == ATT_PENDING
