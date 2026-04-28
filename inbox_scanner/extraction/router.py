"""Decide which extractor (if any) handles a given attachment.

The router is intentionally pure-ish: a small set of mime allowlists and a
PDF text-layer probe. Real OCR / layout analysis happens in the extractors
themselves. Keeping the routing logic small and direct makes it easy to
unit-test without spinning up Docling or a VLM.
"""

from __future__ import annotations

from typing import Literal

import pypdfium2 as pdfium

ExtractionRoute = Literal["docling", "qwen-vl", "unparseable"]

# Image formats Qwen2.5-VL handles natively. Everything else under
# ``image/*`` (svg, gif, tiff, etc.) is too lossy or animated to be worth
# OCR'ing for v1.
VLM_IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",  # non-standard but seen in the wild
        "image/heic",
        "image/heif",
        "image/webp",
    }
)

# Mime types Docling's pipeline natively handles. Includes Office docs and
# common plaintext formats. We pass these through directly even when the
# filename has the wrong extension — Docling's sniffers are robust enough.
DOCLING_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
        "application/msword",  # doc (legacy)
        "text/html",
        "text/plain",
        "text/csv",
        "text/markdown",
    }
)

# Heuristic threshold for "this PDF is born-digital". 100 chars over the
# first three pages reliably distinguishes the real-text case from a scan
# whose ``get_text_range`` returns just the leftover invisible OCR layer
# (often 0–30 chars).
_PDF_TEXT_PAGES_TO_CHECK = 3
_PDF_TEXT_MIN_CHARS = 100


def has_pdf_text_layer(content: bytes) -> bool:
    """Return True if the PDF has a meaningful extractable text layer.

    Reads up to ``_PDF_TEXT_PAGES_TO_CHECK`` pages, accumulating their
    text. Stops early once the threshold is met. Treats parse errors as
    "no text layer" rather than crashing — pypdfium2 can raise on
    corrupted or password-protected PDFs.
    """
    try:
        pdf = pdfium.PdfDocument(content)
    except pdfium.PdfiumError:
        return False
    try:
        text_chars = 0
        n_pages = min(len(pdf), _PDF_TEXT_PAGES_TO_CHECK)
        for i in range(n_pages):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                # ``get_text_bounded`` is the v4 successor to
                # ``get_text_range`` — same semantics with explicit defaults,
                # avoids a UserWarning at runtime.
                page_text = textpage.get_text_bounded()
            finally:
                page.close()
            text_chars += len(page_text.strip())
            if text_chars >= _PDF_TEXT_MIN_CHARS:
                return True
        return False
    finally:
        pdf.close()


def route(mime_type: str | None, content: bytes) -> ExtractionRoute:
    """Decide the extraction route for a single attachment.

    ``content`` is only consulted for PDFs (to probe the text layer);
    callers can pass ``b""`` if they know the mime up front and just want
    the mime-only verdict.
    """
    mime = (mime_type or "application/octet-stream").lower()

    if mime == "application/pdf":
        return "docling" if has_pdf_text_layer(content) else "qwen-vl"

    if mime in VLM_IMAGE_MIME_TYPES:
        return "qwen-vl"

    if mime.startswith("image/"):
        return "unparseable"

    if mime in DOCLING_MIME_TYPES:
        return "docling"

    return "unparseable"
