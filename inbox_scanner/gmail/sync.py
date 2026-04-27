"""Phase 1 sync orchestrator (step 2 scope: metadata only).

Lists messages matching ``has:attachment``, fetches their headers + part
structure via ``format=metadata`` (no body bytes), and writes ``Message``
and ``Attachment`` stub rows to the DB.

Step 3 will:

* swap in async + httpx + a 20 req/sec token bucket and 4 worker tasks,
* download attachment bytes via ``users.messages.attachments.get``,
* hash bytes into content-addressed blob storage,
* flip attachment ``sync_status`` from ``pending`` to ``downloaded``.

Until then this module stays sequential and unrate-limited — fine for the
``--limit 5`` smoke test the build-order milestone calls for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from inbox_scanner.config import Settings
from inbox_scanner.gmail.auth import load_credentials
from inbox_scanner.gmail.client import (
    GmailClient,
    parse_headers,
    parse_received_at,
    walk_attachment_parts,
)
from inbox_scanner.logging import get_logger
from inbox_scanner.models import Attachment, Message, Sync

# Mime types that are almost never useful as PII targets — drop them at sync
# time so we don't waste bytes on the wire (and don't waste extractor cycles
# on them in step 3 either).
SKIP_MIME_TYPES = frozenset(
    {
        "image/gif",
        "text/calendar",
        "application/pkcs7-signature",
        "application/pgp-signature",
    }
)
MIN_ATTACHMENT_SIZE = 1024  # bytes; smaller is almost always a tracking pixel

# Attachment ``sync_status`` values used by this module.
ATT_PENDING = "pending"
ATT_SKIPPED_FILTER = "skipped_filter"
ATT_SKIPPED_TOO_LARGE = "skipped_too_large"

# Message ``sync_status`` values relevant here.
MSG_PENDING = "pending"
MSG_SYNCED = "synced"
MSG_SYNC_ERROR = "sync_error"

log = get_logger("gmail.sync")


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_query(since: str | None) -> str:
    parts = ["has:attachment"]
    if since:
        # CLI validates ISO format; Gmail expects YYYY/MM/DD.
        parts.append(f"after:{since.replace('-', '/')}")
    return " ".join(parts)


def _classify_attachment(mime_type: str, size_bytes: int, max_bytes: int) -> str:
    if mime_type in SKIP_MIME_TYPES:
        return ATT_SKIPPED_FILTER
    if size_bytes < MIN_ATTACHMENT_SIZE:
        return ATT_SKIPPED_FILTER
    if size_bytes > max_bytes:
        return ATT_SKIPPED_TOO_LARGE
    return ATT_PENDING


def _process_message_metadata(
    session: Session,
    msg: Message,
    payload: dict,
    settings: Settings,
) -> int:
    """Populate header fields + attachment rows for one message. Returns the
    number of attachment parts found (after the inline-filter pass)."""
    headers = parse_headers((payload or {}).get("headers"))
    msg.sender = headers.get("from")
    msg.subject = headers.get("subject")
    msg.received_at = parse_received_at(headers.get("date"))

    attachment_parts = list(walk_attachment_parts(payload))
    msg.has_attachments = len(attachment_parts) > 0
    msg.attachment_count = len(attachment_parts)

    # Wipe stale 'pending' rows from a prior interrupted sync; preserve
    # anything already 'downloaded' (real bytes on disk we care about).
    session.query(Attachment).filter(
        Attachment.message_id == msg.id,
        Attachment.sync_status == ATT_PENDING,
    ).delete(synchronize_session=False)

    max_bytes = settings.extraction.max_attachment_bytes
    for part in attachment_parts:
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId") or ""
        # Gmail attachment IDs are unique per-message, not globally. Composite
        # to match the schema's TEXT primary key constraint.
        composite_id = f"{msg.id}:{attachment_id}"
        if session.get(Attachment, composite_id) is not None:
            continue

        mime_type = part.get("mimeType") or "application/octet-stream"
        size_bytes = int(body.get("size") or 0)
        att = Attachment(
            id=composite_id,
            message_id=msg.id,
            filename=part.get("filename") or None,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sync_status=_classify_attachment(mime_type, size_bytes, max_bytes),
        )
        session.add(att)

    return len(attachment_parts)


def run_sync(
    settings: Settings,
    session: Session,
    *,
    limit: int | None = None,
    since: str | None = None,
    progress: Iterable[str] | None = None,  # placeholder; rich UI wired in CLI
) -> int:
    """Run the metadata-only sync. Returns the new ``Sync.id``.

    Idempotent on re-run: messages already in ``synced`` state are left
    alone; ``pending``/``sync_error`` messages are retried.
    """
    creds = load_credentials(settings.token_path)
    client = GmailClient(creds)

    sync = Sync(
        started_at=_utc_naive_now(),
        status="running",
        total_messages=0,
        synced_messages=0,
    )
    session.add(sync)
    session.flush()  # populate sync.id
    sync_id = sync.id
    session.commit()

    log.info("sync.start", sync_id=sync_id, limit=limit, since=since)

    query = _build_query(since)
    seen = 0
    synced = 0

    try:
        for message_id, thread_id in client.list_message_ids(query, max_results=limit):
            seen += 1
            existing = session.get(Message, message_id)
            if existing is not None and existing.sync_status == MSG_SYNCED:
                log.debug("sync.skip_already_synced", message_id=message_id)
                continue

            if existing is None:
                msg = Message(
                    id=message_id,
                    thread_id=thread_id,
                    sync_id=sync_id,
                    sync_status=MSG_PENDING,
                )
                session.add(msg)
            else:
                msg = existing
                msg.sync_id = sync_id
                msg.sync_status = MSG_PENDING
                msg.sync_error = None

            try:
                meta = client.get_message(message_id)
                payload = meta.get("payload") or {}
                _process_message_metadata(session, msg, payload, settings)
                msg.sync_status = MSG_SYNCED
                msg.synced_at = _utc_naive_now()
                synced += 1
                session.commit()
                log.info(
                    "sync.message_done",
                    message_id=message_id,
                    attachments=msg.attachment_count,
                )
            except Exception as e:
                log.exception(
                    "sync.message_error", message_id=message_id, error=str(e)
                )
                msg.sync_status = MSG_SYNC_ERROR
                msg.sync_error = str(e)
                session.commit()

        sync.total_messages = seen
        sync.synced_messages = synced
        sync.finished_at = _utc_naive_now()
        sync.status = "completed"
        session.commit()
        log.info("sync.complete", sync_id=sync_id, seen=seen, synced=synced)
        return sync_id
    except Exception as e:
        log.exception("sync.failed", sync_id=sync_id, error=str(e))
        sync.status = "failed"
        sync.error = str(e)
        sync.finished_at = _utc_naive_now()
        session.commit()
        raise
