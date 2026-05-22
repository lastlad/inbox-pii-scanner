"""Docling adapter for born-digital extraction.

Returns markdown — Docling's default markdown export preserves enough
structure (headings, tables, lists) for downstream PII detection without
forcing us to design a custom intermediate representation.

Singleton ``DocumentConverter``: instantiating one is cheap, but its first
``convert()`` call lazily downloads ~2 GB of layout/table models. We surface
that to the user via a one-time log line on first call.
"""

from __future__ import annotations

import threading
from io import BytesIO

from docling.datamodel.base_models import DocumentStream
from docling.document_converter import DocumentConverter

from inboxaudit.logging import get_logger

log = get_logger("extraction.docling")

_converter_lock = threading.Lock()
_converter: DocumentConverter | None = None
_first_call_warned = False


class DoclingExtractionError(RuntimeError):
    pass


def _get_converter() -> DocumentConverter:
    global _converter
    with _converter_lock:
        if _converter is None:
            _converter = DocumentConverter()
        return _converter


def extract(content: bytes, filename: str) -> str:
    """Extract markdown text from a born-digital document.

    ``filename`` is what Docling uses to sniff the format — pass the
    original attachment filename, not the content-addressed blob path.
    """
    global _first_call_warned
    if not _first_call_warned:
        _first_call_warned = True
        log.info("docling.first_call_may_download_models")

    converter = _get_converter()
    stream = DocumentStream(name=filename, stream=BytesIO(content))
    try:
        result = converter.convert(stream)
    except Exception as e:
        raise DoclingExtractionError(f"docling failed for {filename}: {e}") from e

    if result is None or result.document is None:
        raise DoclingExtractionError(f"docling returned no document for {filename}")
    return result.document.export_to_markdown()
