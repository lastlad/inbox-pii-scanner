"""Tests for inbox_scanner.extraction.router.

The PDF text-layer probe is exercised against tiny synthetic PDFs
generated with pypdfium2 (so the test stays self-contained — no fixture
files to maintain).
"""

from __future__ import annotations

import pypdfium2 as pdfium

from inbox_scanner.extraction.router import (
    DOCLING_MIME_TYPES,
    VLM_IMAGE_MIME_TYPES,
    has_pdf_text_layer,
    route,
)

# ---------- mime → route ----------


def test_jpeg_routes_to_vlm():
    assert route("image/jpeg", b"") == "qwen-vl"


def test_png_routes_to_vlm():
    assert route("image/png", b"") == "qwen-vl"


def test_heic_routes_to_vlm():
    assert route("image/heic", b"") == "qwen-vl"


def test_unknown_image_subtype_unparseable():
    # tiff/svg/etc. — we don't support these in v1.
    assert route("image/svg+xml", b"") == "unparseable"
    assert route("image/tiff", b"") == "unparseable"


def test_docx_routes_to_docling():
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert route(mime, b"") == "docling"


def test_csv_html_plain_route_to_docling():
    for m in ("text/csv", "text/html", "text/plain"):
        assert route(m, b"") == "docling"


def test_unknown_mime_unparseable():
    assert route("application/x-tar", b"") == "unparseable"
    assert route("application/zip", b"") == "unparseable"


def test_none_mime_unparseable():
    assert route(None, b"") == "unparseable"


def test_mime_case_insensitive():
    # Mime normalization should lowercase before matching the allowlists.
    assert route("Image/JPEG", b"") == "qwen-vl"
    assert route(
        "Application/Vnd.OpenXMLFormats-OfficeDocument.WordProcessingML.Document",
        b"",
    ) == "docling"


# ---------- mime allowlist exhaustiveness ----------


def test_vlm_allowlist_covers_common_image_formats():
    # Smoke test that we haven't dropped anything by accident.
    for m in ("image/png", "image/jpeg", "image/heic", "image/heif", "image/webp"):
        assert m in VLM_IMAGE_MIME_TYPES


def test_docling_allowlist_covers_office_and_text():
    must_have = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/html",
        "text/csv",
        "text/plain",
    }
    assert must_have <= DOCLING_MIME_TYPES


# ---------- PDF text-layer probe ----------


def _scan_pdf_bytes() -> bytes:
    """Minimal PDF with no text layer — just an empty page. Stands in for a
    scanned-document PDF whose only "text" would come from OCR."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000010 00000 n \n"
        b"0000000060 00000 n \n"
        b"0000000110 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n"
    )


def test_has_text_layer_false_for_scan_pdf():
    assert has_pdf_text_layer(_scan_pdf_bytes()) is False


def test_has_text_layer_false_for_garbage_bytes():
    # pypdfium2 raises PdfiumError on non-PDF input; router catches it.
    assert has_pdf_text_layer(b"not a pdf at all") is False


def test_pdf_with_no_text_layer_routes_to_vlm():
    """Born-digital PDF routing is exercised by the real scan integration
    test (against actual Receipt.pdf attachments) — synthesizing a valid
    text-layer PDF in pure Python is too fragile to be worth maintaining."""
    assert route("application/pdf", _scan_pdf_bytes()) == "qwen-vl"
    assert route("application/pdf", b"not a pdf") == "qwen-vl"
