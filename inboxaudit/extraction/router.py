"""Decide which extractor (if any) handles a given attachment.

Single-backend design for v1: everything Docling supports goes through
Docling. The earlier two-track plan (Docling for born-digital, Qwen-VL via
``llama-server`` for images and scanned PDFs) was written before Docling
2.x added native image support and on-by-default OCR. Real-attachment
testing (USPS shipping label, marketing JPEGs, PDF receipts) showed
Docling's literal-text OCR output is actually more useful for downstream
PII detection than a VLM's narrative description, so we collapsed.

If we ever need richer image understanding (handwriting, complex charts),
Docling's own ``do_picture_description`` flag wires in SmolVLM as an
opt-in enrichment without bringing back a second HTTP service.
"""

from __future__ import annotations

from typing import Literal

ExtractionRoute = Literal["docling", "unparseable"]

# Mime types Docling 2.x's pipeline natively handles. Sourced from the
# library's own ``MimeTypeToFormat`` mapping at install time and trimmed to
# what we actually expect to see in email attachments. ``image/jpg`` is
# non-standard but turns up in the wild; we normalize it to ``image/jpeg``
# at lookup time.
DOCLING_MIME_TYPES: frozenset[str] = frozenset(
    {
        # Documents
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
        "application/msword",  # legacy doc — Docling tries best-effort
        "text/html",
        "text/plain",
        "text/csv",
        "text/markdown",
        # Images: Docling routes these through its IMAGE InputFormat
        # (OCR via EasyOCR, layout via the bundled layout model).
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/bmp",
        "image/webp",
        # ``image/gif`` is technically supported by Docling but pre-filtered
        # at sync time as low-signal noise; it stays out of the allowlist
        # so it surfaces as 'unparseable' if a future code path stops
        # filtering it.
    }
)

# Mime aliases we normalize before matching the allowlist. Keys are what we
# might see; values are the canonical Docling-recognised mime.
_MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/x-png": "image/png",
}


def _canonicalize(mime_type: str | None) -> str:
    mime = (mime_type or "application/octet-stream").lower().strip()
    return _MIME_ALIASES.get(mime, mime)


def route(mime_type: str | None) -> ExtractionRoute:
    """Decide the extraction route for a single attachment.

    No content sniffing — mime type alone determines the route. Docling's
    pipeline auto-detects whether a PDF is born-digital vs. scanned, so
    we don't need to pre-classify here.
    """
    if _canonicalize(mime_type) in DOCLING_MIME_TYPES:
        return "docling"
    return "unparseable"
