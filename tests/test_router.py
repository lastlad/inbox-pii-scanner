"""Tests for inboxaudit.extraction.router.

The router collapsed to a single Docling allowlist after Docling 2.x added
native image and OCR support — these tests pin the supported mime list and
the canonicalisation behaviour.
"""

from __future__ import annotations

from inboxaudit.extraction.router import DOCLING_MIME_TYPES, route


# ---------- documents ----------


def test_pdf_routes_to_docling():
    # Born-digital vs. scanned routing happens *inside* Docling now
    # (do_ocr=True default), so the router doesn't pre-classify.
    assert route("application/pdf") == "docling"


def test_office_docs_route_to_docling():
    for mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
    ):
        assert route(mime) == "docling", mime


def test_text_formats_route_to_docling():
    for mime in ("text/csv", "text/html", "text/plain", "text/markdown"):
        assert route(mime) == "docling", mime


# ---------- images ----------


def test_supported_images_route_to_docling():
    for mime in (
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/bmp",
        "image/webp",
    ):
        assert route(mime) == "docling", mime


def test_image_jpg_alias_normalized():
    # Some mailers send the non-standard image/jpg; canonicalise to image/jpeg.
    assert route("image/jpg") == "docling"


def test_image_x_png_alias_normalized():
    assert route("image/x-png") == "docling"


def test_unsupported_images_unparseable():
    # Docling 2.x's IMAGE pipeline doesn't list these; we don't fake support.
    for mime in ("image/heic", "image/heif", "image/svg+xml", "image/gif"):
        assert route(mime) == "unparseable", mime


# ---------- everything else ----------


def test_unknown_mimes_unparseable():
    for mime in (
        "application/x-tar",
        "application/zip",
        "application/octet-stream",
        "video/mp4",  # Docling has audio/video paths but we don't enable them
    ):
        assert route(mime) == "unparseable", mime


def test_none_or_empty_mime_unparseable():
    assert route(None) == "unparseable"
    assert route("") == "unparseable"


def test_mime_case_insensitive():
    assert route("Image/JPEG") == "docling"
    assert route(
        "Application/Vnd.OpenXMLFormats-OfficeDocument.WordProcessingML.Document"
    ) == "docling"
    assert route("APPLICATION/PDF") == "docling"


# ---------- allowlist exhaustiveness ----------


def test_docling_allowlist_covers_expected_formats():
    must_have = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/html",
        "text/csv",
        "text/plain",
        "image/png",
        "image/jpeg",
    }
    assert must_have <= DOCLING_MIME_TYPES
