"""Tests for the pure helpers in inboxaudit.gmail.client.

These are the bits most likely to break in production (header weirdness,
nested multipart trees, non-attachment leaf parts) and they don't need a
real Gmail connection — feed in canned API payloads.
"""

from __future__ import annotations

from datetime import datetime

from inboxaudit.gmail.client import (
    parse_headers,
    parse_received_at,
    walk_attachment_parts,
)


# ---------- parse_headers ----------


def test_parse_headers_lowercases_keys():
    out = parse_headers([{"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": "hi"}])
    assert out == {"from": "a@b.com", "subject": "hi"}


def test_parse_headers_handles_none_and_empty():
    assert parse_headers(None) == {}
    assert parse_headers([]) == {}


# ---------- parse_received_at ----------


def test_parse_received_at_rfc2822_with_offset():
    # Tue, 27 Apr 2026 09:00:00 -0700  ->  16:00:00 UTC
    dt = parse_received_at("Tue, 27 Apr 2026 09:00:00 -0700")
    assert dt == datetime(2026, 4, 27, 16, 0, 0)
    # Stored naive (SQLite-friendly).
    assert dt.tzinfo is None


def test_parse_received_at_utc_z():
    dt = parse_received_at("Tue, 27 Apr 2026 16:00:00 +0000")
    assert dt == datetime(2026, 4, 27, 16, 0, 0)


def test_parse_received_at_missing_or_garbage():
    assert parse_received_at(None) is None
    assert parse_received_at("") is None
    assert parse_received_at("not a date") is None


# ---------- walk_attachment_parts ----------


def _attachment_part(filename: str, mime: str, size: int = 4096, att_id: str = "att-1") -> dict:
    return {
        "filename": filename,
        "mimeType": mime,
        "body": {"size": size, "attachmentId": att_id},
    }


def _inline_part(mime: str, size: int = 256) -> dict:
    """Body part with no filename and no attachmentId — e.g. text/plain body
    or an inline image referenced via Content-ID."""
    return {"filename": "", "mimeType": mime, "body": {"size": size, "data": "..."}}


def test_walk_finds_single_attachment():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            _inline_part("text/plain"),
            _attachment_part("scan.pdf", "application/pdf", size=200_000, att_id="A"),
        ],
    }
    found = list(walk_attachment_parts(payload))
    assert [p["filename"] for p in found] == ["scan.pdf"]


def test_walk_skips_inline_no_filename():
    """An inline image with no filename is body content, not an attachment.
    The plan's skip rules call this case out explicitly."""
    payload = {
        "mimeType": "multipart/related",
        "parts": [
            _inline_part("text/html"),
            # Looks attachment-shaped but no filename → skip.
            {
                "filename": "",
                "mimeType": "image/png",
                "body": {"size": 5000, "attachmentId": "inline-1"},
            },
        ],
    }
    assert list(walk_attachment_parts(payload)) == []


def test_walk_descends_nested_multipart():
    """multipart/mixed wrapping multipart/alternative wrapping leaves."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    _inline_part("text/plain"),
                    _inline_part("text/html"),
                ],
            },
            _attachment_part("contract.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", att_id="A"),
            _attachment_part("photo.jpg", "image/jpeg", att_id="B"),
        ],
    }
    found = list(walk_attachment_parts(payload))
    assert [p["filename"] for p in found] == ["contract.docx", "photo.jpg"]


def test_walk_handles_empty_payload():
    assert list(walk_attachment_parts(None)) == []
    assert list(walk_attachment_parts({})) == []


def test_walk_skips_leaf_with_filename_but_no_attachment_id():
    """E.g. text/plain leaves with filename set but body inlined as data —
    not actually a separate attachment to download."""
    payload = {
        "filename": "inline.txt",
        "mimeType": "text/plain",
        "body": {"size": 500, "data": "aGVsbG8="},
    }
    assert list(walk_attachment_parts(payload)) == []
