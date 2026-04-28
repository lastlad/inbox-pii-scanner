"""Phase 2 scan pipeline.

Step 4 scope: **extract stage only**. Iterates downloaded attachments,
routes each to docling / qwen-vl / unparseable, runs the appropriate
extractor (currently only docling), writes extracted markdown to
``<data_dir>/extracted/<content_hash>.md``, and updates the
``Attachment.extraction_*`` columns.

* The qwen-vl route is left at ``extraction_status='pending'`` — step 5
  wires up the VLM and picks those up.
* The unparseable route gets ``extraction_status='unparseable'`` with a
  short reason.
* Extraction is keyed by ``content_hash`` — two attachments with identical
  bytes share one cached ``.md`` file and one extraction call.

The detect stage lands in step 6.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inbox_scanner.blobs import read_blob
from inbox_scanner.config import Settings
from inbox_scanner.db import session_scope
from inbox_scanner.extraction import docling_extractor
from inbox_scanner.extraction.router import route as route_attachment
from inbox_scanner.logging import get_logger
from inbox_scanner.models import Attachment, Scan

log = get_logger("scan")

# Attachment ``extraction_status`` values used here.
EXT_PENDING = "pending"
EXT_EXTRACTED = "extracted"
EXT_UNPARSEABLE = "unparseable"

# Concurrency: Docling holds the GIL for parts of layout analysis but
# releases it during I/O and torch ops, so a small thread pool helps.
# Keep it modest — real OCR (qwen-vl in step 5) will dominate scan time.
_DEFAULT_EXTRACT_CONCURRENCY = 2


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_scan_row(
    session_factory: sessionmaker[Session], config_snapshot: dict
) -> int:
    with session_scope(session_factory) as session:
        scan = Scan(
            started_at=_utc_naive_now(),
            status="running",
            total_attachments=0,
            processed_attachments=0,
            config_snapshot=config_snapshot,
        )
        session.add(scan)
        session.flush()
        return scan.id


def _finalize_scan_row(
    session_factory: sessionmaker[Session],
    scan_id: int,
    *,
    status: str,
    total: int,
    processed: int,
    error: str | None = None,
) -> None:
    with session_scope(session_factory) as session:
        scan = session.get(Scan, scan_id)
        if scan is None:
            return
        scan.status = status
        scan.total_attachments = total
        scan.processed_attachments = processed
        scan.finished_at = _utc_naive_now()
        if error is not None:
            scan.error = error


def _select_extract_work(
    session_factory: sessionmaker[Session],
    *,
    force_extract: bool,
) -> list[dict]:
    """Return the set of attachments that need extraction this scan.

    Each entry is a small dict (not an ORM object) so workers can read it
    without holding a session open. Includes the attachment_id, blob path,
    mime, filename, and content_hash.
    """
    with session_scope(session_factory) as session:
        stmt = (
            select(
                Attachment.id,
                Attachment.blob_path,
                Attachment.mime_type,
                Attachment.filename,
                Attachment.content_hash,
                Attachment.extraction_status,
            )
            .where(Attachment.sync_status == "downloaded")
            .where(Attachment.blob_path.is_not(None))
        )
        if not force_extract:
            # Skip attachments already extracted (or marked unparseable);
            # leave qwen-vl pending so step 5 picks them up automatically.
            stmt = stmt.where(
                (Attachment.extraction_status == EXT_PENDING)
                | (Attachment.extraction_status.is_(None))
            )
        rows = session.execute(stmt).all()
    return [
        {
            "attachment_id": r.id,
            "blob_path": r.blob_path,
            "mime_type": r.mime_type,
            "filename": r.filename or r.id,
            "content_hash": r.content_hash,
        }
        for r in rows
    ]


def _record_extraction(
    session_factory: sessionmaker[Session],
    *,
    attachment_id: str,
    scan_id: int,
    route: str,
    status: str,
    extracted_text_path: str | None,
    error: str | None,
) -> None:
    with session_scope(session_factory) as session:
        att = session.get(Attachment, attachment_id)
        if att is None:
            return
        att.last_scan_id = scan_id
        att.extraction_route = route
        att.extraction_status = status
        att.extracted_text_path = extracted_text_path
        att.extracted_at = _utc_naive_now()
        att.extraction_error = error


def _write_extracted(extracted_dir: Path, content_hash: str, text: str) -> str:
    """Write extracted markdown to ``extracted/<hash>.md``. Returns the
    relative path (so it stays portable in the DB even if the data dir
    moves)."""
    extracted_dir.mkdir(parents=True, exist_ok=True)
    rel = f"{content_hash}.md"
    path = extracted_dir / rel
    if not path.exists():
        # Atomic-ish: write to .tmp then rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.rename(path)
    return rel


def _read_cached_extraction(
    extracted_dir: Path, content_hash: str
) -> str | None:
    path = extracted_dir / f"{content_hash}.md"
    return str(path.relative_to(extracted_dir).name) if path.exists() else None


def _process_one(
    settings: Settings,
    session_factory: sessionmaker[Session],
    scan_id: int,
    item: dict,
) -> str:
    """Process one attachment. Runs in a thread.

    Returns the resulting ``extraction_status`` for logging.
    """
    attachment_id = item["attachment_id"]
    content_hash = item["content_hash"]
    blob_path = item["blob_path"]
    mime_type = item["mime_type"]
    filename = item["filename"]

    # Cache hit: another attachment with identical bytes already produced
    # markdown — reuse it without re-running extraction.
    cached = _read_cached_extraction(settings.extracted_dir, content_hash) if content_hash else None
    if cached is not None:
        _record_extraction(
            session_factory,
            attachment_id=attachment_id,
            scan_id=scan_id,
            route="docling",  # cache hit implies a previous successful run
            status=EXT_EXTRACTED,
            extracted_text_path=cached,
            error=None,
        )
        log.info(
            "extract.cache_hit",
            attachment_id=attachment_id,
            content_hash=content_hash[:12],
        )
        return EXT_EXTRACTED

    try:
        content = read_blob(blob_path, settings.attachments_dir)
    except FileNotFoundError as e:
        _record_extraction(
            session_factory,
            attachment_id=attachment_id,
            scan_id=scan_id,
            route="unparseable",
            status=EXT_UNPARSEABLE,
            extracted_text_path=None,
            error=f"blob missing: {e}",
        )
        log.warning("extract.blob_missing", attachment_id=attachment_id)
        return EXT_UNPARSEABLE

    chosen_route = route_attachment(mime_type, content)

    if chosen_route == "qwen-vl":
        # Step 5 wires this route up — leave the row pending so the next
        # scan run picks it up automatically.
        _record_extraction(
            session_factory,
            attachment_id=attachment_id,
            scan_id=scan_id,
            route="qwen-vl",
            status=EXT_PENDING,
            extracted_text_path=None,
            error=None,
        )
        log.info(
            "extract.deferred_to_vlm",
            attachment_id=attachment_id,
            mime=mime_type,
        )
        return EXT_PENDING

    if chosen_route == "unparseable":
        _record_extraction(
            session_factory,
            attachment_id=attachment_id,
            scan_id=scan_id,
            route="unparseable",
            status=EXT_UNPARSEABLE,
            extracted_text_path=None,
            error=f"no extractor handles mime={mime_type}",
        )
        log.info(
            "extract.unparseable",
            attachment_id=attachment_id,
            mime=mime_type,
        )
        return EXT_UNPARSEABLE

    # docling route
    try:
        text = docling_extractor.extract(content, filename)
    except Exception as e:
        log.exception(
            "extract.docling_failed",
            attachment_id=attachment_id,
            mime=mime_type,
        )
        _record_extraction(
            session_factory,
            attachment_id=attachment_id,
            scan_id=scan_id,
            route="docling",
            status=EXT_UNPARSEABLE,
            extracted_text_path=None,
            error=str(e),
        )
        return EXT_UNPARSEABLE

    rel = _write_extracted(settings.extracted_dir, content_hash, text)
    _record_extraction(
        session_factory,
        attachment_id=attachment_id,
        scan_id=scan_id,
        route="docling",
        status=EXT_EXTRACTED,
        extracted_text_path=rel,
        error=None,
    )
    log.info(
        "extract.docling_done",
        attachment_id=attachment_id,
        chars=len(text),
        content_hash=content_hash[:12],
    )
    return EXT_EXTRACTED


# ---------- top-level orchestrator ----------


async def run_scan(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    force_extract: bool = False,
    only_extract: bool = False,
    only_detect: bool = False,
    extract_concurrency: int = _DEFAULT_EXTRACT_CONCURRENCY,
    on_total_known=None,
    on_attachment_done=None,
) -> int:
    if only_extract and only_detect:
        raise ValueError("only_extract and only_detect are mutually exclusive")

    config_snapshot = {
        "force_extract": force_extract,
        "only_extract": only_extract,
        "only_detect": only_detect,
        "extract_concurrency": extract_concurrency,
    }
    scan_id = await asyncio.to_thread(_create_scan_row, session_factory, config_snapshot)
    log.info("scan.start", scan_id=scan_id, **config_snapshot)

    try:
        if only_detect:
            log.info("scan.skipping_extract", scan_id=scan_id)
            work: list[dict] = []
        else:
            work = await asyncio.to_thread(
                _select_extract_work, session_factory, force_extract=force_extract
            )

        log.info("scan.extract_enumerated", scan_id=scan_id, count=len(work))
        if on_total_known is not None:
            on_total_known(len(work))

        processed = 0
        if work:
            sem = asyncio.Semaphore(extract_concurrency)

            async def _do_one(item: dict) -> None:
                nonlocal processed
                async with sem:
                    try:
                        await asyncio.to_thread(
                            _process_one, settings, session_factory, scan_id, item
                        )
                    except Exception:
                        log.exception(
                            "scan.attachment_failed",
                            attachment_id=item["attachment_id"],
                        )
                    finally:
                        processed += 1
                        if on_attachment_done is not None:
                            on_attachment_done(item["attachment_id"])

            await asyncio.gather(*[_do_one(item) for item in work])

        # Detect stage lands in step 6. Until then `--only-detect` is a no-op
        # and the default scan run completes after extraction.
        if only_extract:
            log.info("scan.only_extract_done", scan_id=scan_id, processed=processed)
        # (no detect-stage call yet)

        await asyncio.to_thread(
            _finalize_scan_row,
            session_factory,
            scan_id,
            status="completed",
            total=len(work),
            processed=processed,
        )
        log.info("scan.complete", scan_id=scan_id, processed=processed)
        return scan_id

    except Exception as e:
        log.exception("scan.failed", scan_id=scan_id)
        await asyncio.to_thread(
            _finalize_scan_row,
            session_factory,
            scan_id,
            status="failed",
            total=0,
            processed=0,
            error=str(e),
        )
        raise
