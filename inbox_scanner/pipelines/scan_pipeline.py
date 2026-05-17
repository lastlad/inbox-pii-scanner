"""Phase 2 scan pipeline.

Two stages:

* **Extract** — iterates downloaded attachments, routes each via the
  :mod:`router <inbox_scanner.extraction.router>`, runs Docling on
  supported mimes, writes markdown to
  ``<data_dir>/extracted/<content_hash>.md``, and updates the
  ``Attachment.extraction_*`` columns. Cached by ``content_hash`` so two
  attachments with identical bytes share one extraction call.

* **Detect** — reads the extracted markdown, runs Presidio + Privacy
  Filter + custom regex via the :mod:`detection runner
  <inbox_scanner.detection.runner>`, and rewrites the ``detections`` and
  ``message_verdicts`` rows for every message touched. Detection results
  are scan-scoped: re-running ``scan`` blows away the prior scan's
  detections for the affected attachments and writes fresh ones, so
  threshold tweaks always reflect the current config.

``--only-extract`` skips stage B (useful while iterating on extractors).
``--only-detect`` skips stage A (useful while iterating on detectors —
extracted markdown is already cached).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from inbox_scanner.blobs import read_blob
from inbox_scanner.config import Settings
from inbox_scanner.db import session_scope
from inbox_scanner.detection import categorizer, runner as detection_runner
from inbox_scanner.detection.types import Detection as DetectionTuple, Finding, Profile
from inbox_scanner.extraction import docling_extractor
from inbox_scanner.extraction.router import route as route_attachment
from inbox_scanner.logging import get_logger
from inbox_scanner.models import Attachment, Detection, Message, MessageVerdict, Scan

log = get_logger("scan")

# Attachment ``extraction_status`` values used here.
EXT_PENDING = "pending"
EXT_EXTRACTED = "extracted"
EXT_UNPARSEABLE = "unparseable"

_DEFAULT_EXTRACT_CONCURRENCY = 2
# Detection runs sequentially per attachment (Presidio + Privacy Filter
# share singletons internally and don't gain from parallelism on CPU);
# keep the knob exposed for future tuning.
_DEFAULT_DETECT_CONCURRENCY = 1


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

    chosen_route = route_attachment(mime_type)

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


# ---------- detect stage helpers ----------


def _select_detect_work(
    session_factory: sessionmaker[Session],
) -> list[dict]:
    """Return attachments with cached extracted text. Detection always runs
    over the full set (per-scan rewrite); there's no ``force_detect`` knob."""
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(
                Attachment.id,
                Attachment.message_id,
                Attachment.extracted_text_path,
                Attachment.content_hash,
                Attachment.filename,
            )
            .where(Attachment.extraction_status == EXT_EXTRACTED)
            .where(Attachment.extracted_text_path.is_not(None))
        ).all()
    return [
        {
            "attachment_id": r.id,
            "message_id": r.message_id,
            "extracted_text_path": r.extracted_text_path,
            "content_hash": r.content_hash,
            "filename": r.filename,
        }
        for r in rows
    ]


def _run_detection_for_attachment(
    settings: Settings, item: dict, profile: Profile
) -> list[DetectionTuple]:
    """Read the cached markdown and run all three detectors. Returns
    categorized detections; the caller persists them."""
    rel = item["extracted_text_path"]
    if not rel:
        return []
    text_path = settings.extracted_dir / rel
    if not text_path.is_file():
        log.warning("detect.missing_extracted_text", path=str(text_path))
        return []
    text = text_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []
    return detection_runner.run(
        text,
        presidio_threshold=settings.detection.presidio_threshold,
        privacy_filter_threshold=settings.detection.privacy_filter_threshold,
        profile=profile,
    )


def _persist_detections(
    session_factory: sessionmaker[Session],
    scan_id: int,
    attachment_id: str,
    detections: list[DetectionTuple],
) -> None:
    """Replace this attachment's detection rows with the given list.

    Per the plan, ``scan`` is a full overwrite for the affected scope —
    we drop any prior detection rows for this attachment (regardless of
    which scan produced them) and write the current run's findings.
    """
    with session_scope(session_factory) as session:
        session.execute(
            delete(Detection).where(Detection.attachment_id == attachment_id)
        )
        now = _utc_naive_now()
        for d in detections:
            f = d.finding
            session.add(
                Detection(
                    scan_id=scan_id,
                    attachment_id=attachment_id,
                    category=d.category,
                    subtype=f.subtype,
                    detector=f.detector,
                    span_text=f.span_text[:500] if f.span_text else None,
                    span_start=f.span_start,
                    span_end=f.span_end,
                    confidence=f.confidence,
                    created_at=now,
                )
            )


def _compute_and_persist_verdicts(
    session_factory: sessionmaker[Session],
    scan_id: int,
    affected_message_ids: set[str],
) -> int:
    """Aggregate per-message verdicts from the just-written detection rows
    and write/upsert the ``message_verdicts`` rows. Returns the number of
    flagged messages."""
    if not affected_message_ids:
        return 0

    flagged = 0
    with session_scope(session_factory) as session:
        for message_id in affected_message_ids:
            # Pull every detection currently on disk for any attachment
            # belonging to this message — re-aggregating lets the verdict
            # remain accurate even if some attachments weren't touched
            # this scan (e.g. they were already extracted from a prior
            # scan and we ran ``--only-detect`` on a subset).
            rows = session.execute(
                select(
                    Detection.category,
                    Detection.subtype,
                    Detection.detector,
                    Detection.span_text,
                    Detection.span_start,
                    Detection.span_end,
                    Detection.confidence,
                )
                .join(Attachment, Attachment.id == Detection.attachment_id)
                .where(Attachment.message_id == message_id)
            ).all()

            verdict = categorizer.compute_verdict(
                [
                    DetectionTuple(
                        finding=Finding(
                            detector=r.detector,
                            subtype=r.subtype,
                            span_text=r.span_text or "",
                            span_start=r.span_start or 0,
                            span_end=r.span_end or 0,
                            confidence=r.confidence or 0.0,
                        ),
                        category=r.category,
                    )
                    for r in rows
                ]
            )

            # Upsert: delete then add (SQLite has no portable UPSERT in
            # SQLAlchemy 2.0 outside dialect-specific INSERT … ON CONFLICT,
            # and the verdict row count is bounded by inbox size).
            session.execute(
                delete(MessageVerdict).where(MessageVerdict.message_id == message_id)
            )
            session.add(
                MessageVerdict(
                    message_id=message_id,
                    scan_id=scan_id,
                    is_flagged=verdict["is_flagged"],
                    top_category=verdict["top_category"],
                    risk_score=verdict["risk_score"],
                    category_summary=verdict["category_summary"],
                )
            )
            if verdict["is_flagged"]:
                flagged += 1
    return flagged


# ---------- top-level orchestrator ----------


async def run_scan(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    force_extract: bool = False,
    only_extract: bool = False,
    only_detect: bool = False,
    profile: Profile = Profile.CRITICAL,
    extract_concurrency: int = _DEFAULT_EXTRACT_CONCURRENCY,
    detect_concurrency: int = _DEFAULT_DETECT_CONCURRENCY,
    on_extract_total_known=None,
    on_extract_done=None,
    on_detect_total_known=None,
    on_detect_done=None,
) -> int:
    if only_extract and only_detect:
        raise ValueError("only_extract and only_detect are mutually exclusive")

    config_snapshot = {
        "force_extract": force_extract,
        "only_extract": only_extract,
        "only_detect": only_detect,
        "profile": profile.value,
        "extract_concurrency": extract_concurrency,
        "detect_concurrency": detect_concurrency,
        "presidio_threshold": settings.detection.presidio_threshold,
        "privacy_filter_threshold": settings.detection.privacy_filter_threshold,
    }
    scan_id = await asyncio.to_thread(_create_scan_row, session_factory, config_snapshot)
    log.info("scan.start", scan_id=scan_id, **config_snapshot)

    try:
        # ---------- Stage A: extract ----------
        if only_detect:
            log.info("scan.skipping_extract", scan_id=scan_id)
            work: list[dict] = []
        else:
            work = await asyncio.to_thread(
                _select_extract_work, session_factory, force_extract=force_extract
            )

        log.info("scan.extract_enumerated", scan_id=scan_id, count=len(work))
        if on_extract_total_known is not None:
            on_extract_total_known(len(work))

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
                        if on_extract_done is not None:
                            on_extract_done(item["attachment_id"])

            await asyncio.gather(*[_do_one(item) for item in work])

        # ---------- Stage B: detect ----------
        flagged = 0
        detect_processed = 0
        detect_total = 0
        if only_extract:
            log.info("scan.only_extract_done", scan_id=scan_id, processed=processed)
        else:
            detect_work = await asyncio.to_thread(_select_detect_work, session_factory)
            detect_total = len(detect_work)
            log.info(
                "scan.detect_enumerated", scan_id=scan_id, count=detect_total
            )
            if on_detect_total_known is not None:
                on_detect_total_known(detect_total)

            affected_message_ids: set[str] = set()
            if detect_work:
                sem_d = asyncio.Semaphore(detect_concurrency)

                async def _detect_one(item: dict) -> None:
                    nonlocal detect_processed
                    async with sem_d:
                        try:
                            detections = await asyncio.to_thread(
                                _run_detection_for_attachment,
                                settings,
                                item,
                                profile,
                            )
                            await asyncio.to_thread(
                                _persist_detections,
                                session_factory,
                                scan_id,
                                item["attachment_id"],
                                detections,
                            )
                            affected_message_ids.add(item["message_id"])
                        except Exception:
                            log.exception(
                                "scan.detect_failed",
                                attachment_id=item["attachment_id"],
                            )
                        finally:
                            detect_processed += 1
                            if on_detect_done is not None:
                                on_detect_done(item["attachment_id"])

                await asyncio.gather(*[_detect_one(item) for item in detect_work])

            flagged = await asyncio.to_thread(
                _compute_and_persist_verdicts,
                session_factory,
                scan_id,
                affected_message_ids,
            )
            log.info(
                "scan.detect_done",
                scan_id=scan_id,
                processed=detect_processed,
                messages_affected=len(affected_message_ids),
                flagged_messages=flagged,
            )

        # The Scan row's ``processed_attachments`` is meant to answer "how
        # many attachments did this scan touch". Take the max across the
        # two stages so ``--only-detect`` runs report their detect count
        # (extract was 0) and full runs report the larger working set.
        scan_processed = max(processed, detect_processed)
        scan_total = max(len(work), detect_total)
        await asyncio.to_thread(
            _finalize_scan_row,
            session_factory,
            scan_id,
            status="completed",
            total=scan_total,
            processed=scan_processed,
        )
        log.info(
            "scan.complete",
            scan_id=scan_id,
            extract_processed=processed,
            detect_processed=detect_processed,
            flagged=flagged,
        )
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
