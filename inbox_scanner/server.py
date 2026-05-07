"""FastAPI app — read-only JSON over the local SQLite store.

The plan's API surface is minimal on purpose:

* ``GET /api/stats`` — sync + scan summary for the dashboard.
* ``GET /api/flagged`` — paginated list of flagged messages with
  enough metadata to render a row (sender / subject / received_at /
  risk_score / category_summary / gmail_url).
* ``GET /api/email/{message_id}`` — full detail for the review pane:
  message metadata, every attachment, every detection per attachment,
  and a ±200-character snippet of the cached extracted text around each
  finding so the UI can show the matched span in context.
* ``GET /`` — serves the static ``frontend/index.html`` (Alpine.js app
  in step 8 — for now this returns a "step 8 lands the UI" stub so a
  bare browser visit is informative).

The server **never** mutates state. It binds to ``127.0.0.1`` only and
ships no auth — single-user local tool — and the CLI prints a loud
warning if the operator overrides ``--host``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from inbox_scanner.config import Settings, load_settings
from inbox_scanner.db import make_engine, make_session_factory, session_scope
from inbox_scanner.models import (
    Attachment,
    Detection,
    Message,
    MessageVerdict,
    Scan,
    Sync,
)

# Default snippet window around each finding (chars on each side). The
# plan calls for ±200 chars; keep it tunable so the UI can ask for less
# if it wants tighter rendering.
DEFAULT_SNIPPET_WINDOW = 200

# Gmail's mailbox-locator URL. ``#all/`` is the safest fragment because
# it works whether the user has archived/labelled the message or it's
# still in INBOX.
_GMAIL_URL_TEMPLATE = "https://mail.google.com/mail/u/0/#all/{message_id}"


# ---------- Pydantic response models ----------


class SyncStats(BaseModel):
    last_sync_at: datetime | None
    last_sync_status: str | None
    total_messages: int
    total_attachments: int
    total_attachments_downloaded: int
    total_blob_bytes: int


class ScanStats(BaseModel):
    last_scan_at: datetime | None
    last_scan_status: str | None
    total_messages_with_verdict: int
    total_flagged: int
    total_findings: int
    by_top_category: dict[str, int]


class StatsResponse(BaseModel):
    sync: SyncStats
    scan: ScanStats


class FlaggedSummary(BaseModel):
    message_id: str
    sender: str | None
    subject: str | None
    received_at: datetime | None
    is_flagged: bool
    top_category: str | None
    risk_score: float
    category_summary: dict[str, int]
    attachment_count: int | None
    gmail_url: str


class FlaggedListResponse(BaseModel):
    items: list[FlaggedSummary]
    next_cursor: str | None
    total: int


class FindingDetail(BaseModel):
    detector: str
    subtype: str
    category: str
    span_text: str | None
    span_start: int | None
    span_end: int | None
    confidence: float | None
    snippet: str | None
    snippet_relative_start: int | None
    snippet_relative_end: int | None


class AttachmentDetail(BaseModel):
    attachment_id: str
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    sync_status: str
    extraction_status: str | None
    extraction_route: str | None
    extraction_error: str | None
    findings: list[FindingDetail]


class EmailDetailResponse(BaseModel):
    message_id: str
    sender: str | None
    subject: str | None
    received_at: datetime | None
    sync_status: str
    is_flagged: bool
    top_category: str | None
    risk_score: float | None
    category_summary: dict[str, int]
    gmail_url: str
    attachments: list[AttachmentDetail]


# ---------- helpers ----------


def _gmail_url(message_id: str) -> str:
    return _GMAIL_URL_TEMPLATE.format(message_id=message_id)


def _blob_bytes_total(attachments_dir: Path) -> int:
    blobs = attachments_dir / "blobs"
    if not blobs.is_dir():
        return 0
    return sum(p.stat().st_size for p in blobs.rglob("*") if p.is_file())


def _snippet_for_finding(
    text: str, start: int | None, end: int | None, window: int
) -> tuple[str | None, int | None, int | None]:
    """Return ``(snippet, rel_start, rel_end)`` — a ±window char window
    around ``[start:end]`` plus the offsets of the original span within
    that snippet. ``None`` if span info is missing."""
    if start is None or end is None or not text:
        return None, None, None
    s = max(0, start - window)
    e = min(len(text), end + window)
    snippet = text[s:e]
    return snippet, start - s, end - s


# ---------- app factory ----------


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    """Construct a FastAPI app bound to one ``Settings`` + DB.

    All three DI parameters are exposed for testability — tests can pass
    a sqlite-backed engine pointing at a tmpdir.
    """
    settings = settings or load_settings()
    if engine is None:
        engine = make_engine(settings.db_path)
    if session_factory is None:
        session_factory = make_session_factory(engine)

    # Quiet uvicorn's own access-log spam during development. The user
    # is staring at the rich progress bar / log output of other commands
    # in adjacent terminals; we don't want every page load echoing back.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    app = FastAPI(
        title="Inbox PII Scanner",
        description=(
            "Read-only local API over the cached scan results. "
            "Bind to 127.0.0.1 only — there is no auth."
        ),
    )

    def _session() -> Iterable[Session]:
        with session_scope(session_factory) as s:
            yield s

    # ---------- routes ----------

    @app.get("/api/stats", response_model=StatsResponse)
    def get_stats(session: Session = Depends(_session)) -> StatsResponse:
        last_sync = session.execute(
            select(Sync).order_by(Sync.started_at.desc()).limit(1)
        ).scalar_one_or_none()
        last_scan = session.execute(
            select(Scan).order_by(Scan.started_at.desc()).limit(1)
        ).scalar_one_or_none()

        total_messages = session.scalar(select(func.count()).select_from(Message)) or 0
        total_attachments = (
            session.scalar(select(func.count()).select_from(Attachment)) or 0
        )
        total_downloaded = (
            session.scalar(
                select(func.count())
                .select_from(Attachment)
                .where(Attachment.sync_status == "downloaded")
            )
            or 0
        )
        total_with_verdict = (
            session.scalar(select(func.count()).select_from(MessageVerdict)) or 0
        )
        total_flagged = (
            session.scalar(
                select(func.count())
                .select_from(MessageVerdict)
                .where(MessageVerdict.is_flagged.is_(True))
            )
            or 0
        )
        total_findings = (
            session.scalar(select(func.count()).select_from(Detection)) or 0
        )

        by_cat = dict(
            session.execute(
                select(MessageVerdict.top_category, func.count())
                .where(MessageVerdict.is_flagged.is_(True))
                .group_by(MessageVerdict.top_category)
            ).all()
        )

        return StatsResponse(
            sync=SyncStats(
                last_sync_at=last_sync.started_at if last_sync else None,
                last_sync_status=last_sync.status if last_sync else None,
                total_messages=total_messages,
                total_attachments=total_attachments,
                total_attachments_downloaded=total_downloaded,
                total_blob_bytes=_blob_bytes_total(settings.attachments_dir),
            ),
            scan=ScanStats(
                last_scan_at=last_scan.started_at if last_scan else None,
                last_scan_status=last_scan.status if last_scan else None,
                total_messages_with_verdict=total_with_verdict,
                total_flagged=total_flagged,
                total_findings=total_findings,
                by_top_category={k or "?": v for k, v in by_cat.items()},
            ),
        )

    @app.get("/api/flagged", response_model=FlaggedListResponse)
    def get_flagged(
        cursor: int = Query(0, ge=0, description="Offset for pagination."),
        limit: int = Query(20, ge=1, le=100),
        category: str | None = Query(
            None,
            description=(
                "Filter to flagged messages whose top_category matches "
                "(gov_id, financial, tax, medical, credentials, legal)."
            ),
        ),
        sort: str = Query(
            "risk",
            pattern="^(risk|date)$",
            description="``risk`` (highest first) or ``date`` (newest first).",
        ),
        session: Session = Depends(_session),
    ) -> FlaggedListResponse:
        # Base query: flagged messages joined to their verdict.
        stmt = (
            select(MessageVerdict, Message)
            .join(Message, Message.id == MessageVerdict.message_id)
            .where(MessageVerdict.is_flagged.is_(True))
        )
        if category:
            stmt = stmt.where(MessageVerdict.top_category == category)

        if sort == "risk":
            stmt = stmt.order_by(
                MessageVerdict.risk_score.desc(),
                Message.received_at.desc(),
            )
        else:  # date
            stmt = stmt.order_by(Message.received_at.desc())

        total_stmt = (
            select(func.count())
            .select_from(MessageVerdict)
            .where(MessageVerdict.is_flagged.is_(True))
        )
        if category:
            total_stmt = total_stmt.where(MessageVerdict.top_category == category)
        total = session.scalar(total_stmt) or 0

        rows = session.execute(stmt.offset(cursor).limit(limit)).all()
        items = [
            FlaggedSummary(
                message_id=v.message_id,
                sender=m.sender,
                subject=m.subject,
                received_at=m.received_at,
                is_flagged=v.is_flagged,
                top_category=v.top_category,
                risk_score=v.risk_score or 0.0,
                category_summary=v.category_summary or {},
                attachment_count=m.attachment_count,
                gmail_url=_gmail_url(m.id),
            )
            for v, m in rows
        ]
        next_cursor = (
            str(cursor + len(items)) if cursor + len(items) < total else None
        )
        return FlaggedListResponse(items=items, next_cursor=next_cursor, total=total)

    @app.get("/api/email/{message_id}", response_model=EmailDetailResponse)
    def get_email(
        message_id: str,
        snippet_window: int = Query(
            DEFAULT_SNIPPET_WINDOW, ge=0, le=2000,
            description="Chars of context to include on each side of every finding.",
        ),
        session: Session = Depends(_session),
    ) -> EmailDetailResponse:
        msg = session.get(Message, message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="message not found")

        verdict = session.get(MessageVerdict, message_id)
        attachments = session.execute(
            select(Attachment).where(Attachment.message_id == message_id)
        ).scalars().all()

        # Pre-load the extracted text once per attachment so we don't
        # re-read the .md file for each finding.
        attachment_text_cache: dict[str, str] = {}
        for a in attachments:
            if a.extraction_status == "extracted" and a.extracted_text_path:
                p = settings.extracted_dir / a.extracted_text_path
                if p.is_file():
                    try:
                        attachment_text_cache[a.id] = p.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pass

        attachment_payloads: list[AttachmentDetail] = []
        for a in attachments:
            findings = session.execute(
                select(Detection)
                .where(Detection.attachment_id == a.id)
                .order_by(Detection.span_start.asc().nulls_last())
            ).scalars().all()

            text = attachment_text_cache.get(a.id, "")
            finding_payloads: list[FindingDetail] = []
            for d in findings:
                snippet, rs, re_ = _snippet_for_finding(
                    text, d.span_start, d.span_end, snippet_window
                )
                finding_payloads.append(
                    FindingDetail(
                        detector=d.detector,
                        subtype=d.subtype,
                        category=d.category,
                        span_text=d.span_text,
                        span_start=d.span_start,
                        span_end=d.span_end,
                        confidence=d.confidence,
                        snippet=snippet,
                        snippet_relative_start=rs,
                        snippet_relative_end=re_,
                    )
                )
            attachment_payloads.append(
                AttachmentDetail(
                    attachment_id=a.id,
                    filename=a.filename,
                    mime_type=a.mime_type,
                    size_bytes=a.size_bytes,
                    sync_status=a.sync_status,
                    extraction_status=a.extraction_status,
                    extraction_route=a.extraction_route,
                    extraction_error=a.extraction_error,
                    findings=finding_payloads,
                )
            )

        return EmailDetailResponse(
            message_id=msg.id,
            sender=msg.sender,
            subject=msg.subject,
            received_at=msg.received_at,
            sync_status=msg.sync_status,
            is_flagged=bool(verdict.is_flagged) if verdict else False,
            top_category=verdict.top_category if verdict else None,
            risk_score=verdict.risk_score if verdict else None,
            category_summary=(verdict.category_summary or {}) if verdict else {},
            gmail_url=_gmail_url(msg.id),
            attachments=attachment_payloads,
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        # The Alpine.js app lands in step 8. Serve it from disk if
        # present so step 8's contributors can iterate without touching
        # this module; until then, return a small inline placeholder
        # that still calls the API so a fresh user can confirm the
        # backend is alive.
        frontend_index = (
            Path(__file__).parent / "frontend" / "index.html"
        )
        if frontend_index.is_file():
            return HTMLResponse(frontend_index.read_text(encoding="utf-8"))
        return HTMLResponse(_PLACEHOLDER_HTML)

    return app


_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Inbox PII Scanner</title>
  <style>
    body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
           max-width: 720px; margin: 4rem auto; padding: 0 1rem;
           color: #1a1a1a; }
    h1 { font-size: 1.25rem; margin-bottom: 0.25rem; }
    p  { color: #555; }
    code { background: #f4f4f4; padding: 0.1rem 0.3rem; border-radius: 4px; }
    .grid { display: grid; grid-template-columns: max-content 1fr;
            gap: 0.5rem 1rem; margin-top: 1.5rem; font-size: 0.9rem; }
    a { color: #0a58ca; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Inbox PII Scanner — local server</h1>
  <p>The review UI lands in build-step 8. Until then, the JSON API is live:</p>
  <div class="grid">
    <a href="/api/stats">/api/stats</a>
    <span>sync + scan summary</span>
    <a href="/api/flagged?limit=20&sort=risk">/api/flagged?limit=20&sort=risk</a>
    <span>flagged messages, paginated</span>
    <a href="/docs">/docs</a>
    <span>OpenAPI / Swagger UI</span>
  </div>
</body>
</html>
"""
