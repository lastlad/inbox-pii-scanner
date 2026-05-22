"""Phase 1 sync orchestrator.

Pulls Gmail messages matching ``has:attachment``, downloads their attachment
bytes into content-addressed blob storage, and writes ``messages`` /
``attachments`` rows. Idempotent on re-run — see resume semantics below.

Concurrency model
-----------------

* One ``asyncio`` event loop drives the whole sync.
* Gmail HTTP calls themselves stay in the (sync) ``googleapiclient`` library
  and are dispatched via :func:`asyncio.to_thread`.
* A single shared :class:`TokenBucket` (default 20 req/sec) sits in front of
  every Gmail call — list, get, attachments.get all draw from the same
  bucket, matching the per-user quota the plan calls for.
* ``concurrency`` (default 4) worker tasks pull messages off an
  :class:`asyncio.Queue`. Each worker has its own :class:`GmailClient` and
  its own DB session pool; SQLite WAL handles the concurrent writes.

Resume semantics
----------------

A re-run picks up cleanly from any state:

* New messages we haven't seen → process from scratch.
* ``sync_status='pending' | 'sync_error'`` → re-fetch metadata + download
  remaining attachments.
* ``sync_status='synced'`` AND no attachment is still in ``pending`` →
  skipped entirely.
* ``sync_status='synced'`` AND some attachment is still ``pending`` →
  download just those (metadata is already captured; we re-fetch only as a
  cheap idempotent safety net).

Blob storage is idempotent by SHA-256 — re-running on a partially-downloaded
attachment doesn't duplicate bytes on disk.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inboxaudit.blobs import store_blob
from inboxaudit.config import Settings
from inboxaudit.db import session_scope
from inboxaudit.gmail.auth import load_credentials
from inboxaudit.gmail.client import (
    GmailClient,
    make_composite_attachment_id,
    parse_headers,
    parse_received_at,
    walk_attachment_parts,
)
from inboxaudit.gmail.rate_limiter import TokenBucket
from inboxaudit.logging import get_logger
from inboxaudit.models import Attachment, Message, Sync

# Mime types that are almost never useful as PII targets — drop them at sync
# time so we don't waste bytes on the wire.
SKIP_MIME_TYPES = frozenset(
    {
        "image/gif",
        "text/calendar",
        "application/pkcs7-signature",
        "application/pgp-signature",
    }
)
MIN_ATTACHMENT_SIZE = 1024  # bytes; smaller is almost always a tracking pixel

# Attachment ``sync_status`` values.
ATT_PENDING = "pending"
ATT_DOWNLOADED = "downloaded"
ATT_SKIPPED_FILTER = "skipped_filter"
ATT_SKIPPED_TOO_LARGE = "skipped_too_large"
ATT_SYNC_ERROR = "sync_error"

# Message ``sync_status`` values.
MSG_PENDING = "pending"
MSG_SYNCED = "synced"
MSG_SYNC_ERROR = "sync_error"

# HTTP status codes worth retrying.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

log = get_logger("gmail.sync")


class MailboxScope(str, Enum):
    """Which subset of the user's mail to scan.

    ``ALL`` is the default and matches the original behaviour: bare
    ``has:attachment`` matches mail across every label except spam/trash
    (so inbox + sent + archive are all included). The other two narrow
    that down to the named label only — useful when the user wants to
    focus on, say, sensitive documents they've *sent* (which often
    matters more than what was sent *to* them).
    """

    ALL = "all"
    INBOX = "inbox"
    SENT = "sent"


# ---------- pure helpers ----------


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_query(since: str | None, mailbox: MailboxScope = MailboxScope.ALL) -> str:
    parts = ["has:attachment"]
    if mailbox == MailboxScope.INBOX:
        parts.append("in:inbox")
    elif mailbox == MailboxScope.SENT:
        parts.append("in:sent")
    # ALL adds no additional filter — bare ``has:attachment`` already
    # matches every label except spam/trash.
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
    payload: dict[str, Any],
    settings: Settings,
) -> int:
    """Populate header fields + attachment rows for one message.

    Returns the count of attachment parts found.
    """
    headers = parse_headers((payload or {}).get("headers"))
    msg.sender = headers.get("from")
    msg.subject = headers.get("subject")
    msg.received_at = parse_received_at(headers.get("date"))

    attachment_parts = list(walk_attachment_parts(payload))
    msg.has_attachments = len(attachment_parts) > 0
    msg.attachment_count = len(attachment_parts)

    max_bytes = settings.extraction.max_attachment_bytes
    for part in attachment_parts:
        body = part.get("body") or {}
        gmail_attachment_id = body.get("attachmentId") or ""
        part_id = part.get("partId") or ""
        if not gmail_attachment_id or not part_id:
            # Defensive: walk_attachment_parts guarantees attachmentId, and
            # Gmail always populates partId for non-root parts. Skip rather
            # than crash if either is missing.
            continue

        composite_id = make_composite_attachment_id(msg.id, part_id)
        existing = session.get(Attachment, composite_id)
        if existing is not None:
            # Refresh the volatile gmail_attachment_id; it may have rotated
            # since the last sync. Status / blob / hash are preserved.
            existing.gmail_attachment_id = gmail_attachment_id
            continue

        mime_type = part.get("mimeType") or "application/octet-stream"
        size_bytes = int(body.get("size") or 0)
        att = Attachment(
            id=composite_id,
            message_id=msg.id,
            part_id=part_id,
            gmail_attachment_id=gmail_attachment_id,
            filename=part.get("filename") or None,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sync_status=_classify_attachment(mime_type, size_bytes, max_bytes),
        )
        session.add(att)

    return len(attachment_parts)


# ---------- async building blocks ----------


async def _gmail_call_with_retry(
    bucket: TokenBucket,
    fn,
    *args,
    max_attempts: int = 5,
    label: str = "gmail.call",
):
    """Run a sync Gmail call inside a thread, with rate limit + retry.

    Retries on 429 and 5xx with exponential backoff + jitter. Re-raises on
    the final attempt or for non-retryable errors.
    """
    for attempt in range(max_attempts):
        await bucket.acquire()
        try:
            return await asyncio.to_thread(fn, *args)
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status not in RETRYABLE_STATUSES or attempt == max_attempts - 1:
                raise
            wait = (2**attempt) + random.uniform(0, 1)
            log.warning(
                f"{label}.retry",
                status=status,
                attempt=attempt + 1,
                wait_s=round(wait, 2),
            )
            await asyncio.sleep(wait)
    raise RuntimeError(f"unreachable: exhausted retries for {label}")


def _list_messages_page(client: GmailClient, query: str, page_token: str | None, page_size: int):
    return (
        client._service.users()  # noqa: SLF001 — internal access acceptable here
        .messages()
        .list(userId="me", q=query, pageToken=page_token, maxResults=page_size)
        .execute()
    )


async def _enumerate_message_ids(
    bucket: TokenBucket,
    client: GmailClient,
    query: str,
    limit: int | None,
) -> list[tuple[str, str]]:
    """Return ``[(message_id, thread_id), ...]`` matching ``query``.

    Paginates serially through ``messages.list``; every page goes through
    the rate limiter. For typical inboxes a 50K-message list is ~100 pages
    so this finishes in a few seconds.
    """
    out: list[tuple[str, str]] = []
    page_token: str | None = None
    while True:
        page_size = 500
        if limit is not None:
            remaining = limit - len(out)
            if remaining <= 0:
                break
            page_size = min(page_size, remaining)
        resp = await _gmail_call_with_retry(
            bucket,
            _list_messages_page,
            client,
            query,
            page_token,
            page_size,
            label="messages.list",
        )
        for m in resp.get("messages", []) or []:
            out.append((m["id"], m["threadId"]))
            if limit is not None and len(out) >= limit:
                return out
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


# ---------- DB sync helpers (run inside asyncio.to_thread) ----------


def _filter_needs_work(
    session_factory: sessionmaker[Session],
    candidate_ids: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return only messages that aren't fully synced yet."""
    if not candidate_ids:
        return []
    ids_only = [m for m, _ in candidate_ids]
    with session_scope(session_factory) as session:
        # Fully done = message synced AND zero pending attachments.
        # Anything else needs work.
        synced = {
            m
            for (m,) in session.execute(
                select(Message.id).where(
                    Message.id.in_(ids_only),
                    Message.sync_status == MSG_SYNCED,
                )
            ).all()
        }
        if not synced:
            return list(candidate_ids)
        with_pending = {
            m
            for (m,) in session.execute(
                select(Attachment.message_id)
                .where(Attachment.message_id.in_(synced))
                .where(Attachment.sync_status == ATT_PENDING)
                .distinct()
            ).all()
        }
    fully_done = synced - with_pending
    return [(m, t) for m, t in candidate_ids if m not in fully_done]


def _persist_message_stub(
    session_factory: sessionmaker[Session],
    settings: Settings,
    message_id: str,
    thread_id: str,
    sync_id: int,
    payload: dict[str, Any],
) -> list[tuple[str, str]]:
    """Upsert message + attachment stubs.

    Returns ``[(composite_id, gmail_attachment_id), ...]`` for attachments
    that still need to be downloaded — the worker passes the
    ``gmail_attachment_id`` straight to ``attachments.get`` while it's still
    fresh.
    """
    with session_scope(session_factory) as session:
        msg = session.get(Message, message_id)
        if msg is None:
            msg = Message(
                id=message_id,
                thread_id=thread_id,
                sync_id=sync_id,
                sync_status=MSG_PENDING,
            )
            session.add(msg)
        else:
            msg.sync_id = sync_id
            msg.sync_status = MSG_PENDING
            msg.sync_error = None

        _process_message_metadata(session, msg, payload, settings)
        session.flush()

        pending = session.execute(
            select(Attachment.id, Attachment.gmail_attachment_id)
            .where(Attachment.message_id == message_id)
            .where(Attachment.sync_status == ATT_PENDING)
        ).all()
        return [(cid, gid) for (cid, gid) in pending if gid]


def _persist_attachment_blob(
    session_factory: sessionmaker[Session],
    settings: Settings,
    composite_id: str,
    content: bytes,
) -> None:
    digest, rel_path = store_blob(content, settings.attachments_dir)
    with session_scope(session_factory) as session:
        att = session.get(Attachment, composite_id)
        if att is None:
            return
        att.content_hash = digest
        att.blob_path = str(rel_path)
        att.sync_status = ATT_DOWNLOADED
        att.downloaded_at = _utc_naive_now()


def _mark_attachment_error(
    session_factory: sessionmaker[Session],
    composite_id: str,
    error: str,
) -> None:
    with session_scope(session_factory) as session:
        att = session.get(Attachment, composite_id)
        if att is not None:
            att.sync_status = ATT_SYNC_ERROR
            att.sync_error = error


def _mark_message_synced(
    session_factory: sessionmaker[Session], message_id: str
) -> None:
    with session_scope(session_factory) as session:
        msg = session.get(Message, message_id)
        if msg is not None:
            msg.sync_status = MSG_SYNCED
            msg.synced_at = _utc_naive_now()


def _mark_message_error(
    session_factory: sessionmaker[Session], message_id: str, error: str
) -> None:
    with session_scope(session_factory) as session:
        msg = session.get(Message, message_id)
        if msg is not None:
            msg.sync_status = MSG_SYNC_ERROR
            msg.sync_error = error


def _create_sync_row(
    session_factory: sessionmaker[Session],
    mailbox: MailboxScope = MailboxScope.ALL,
) -> int:
    with session_scope(session_factory) as session:
        sync = Sync(
            started_at=_utc_naive_now(),
            status="running",
            total_messages=0,
            synced_messages=0,
            mailbox_scope=mailbox.value,
        )
        session.add(sync)
        session.flush()
        return sync.id


def _finalize_sync_row(
    session_factory: sessionmaker[Session],
    sync_id: int,
    *,
    status: str,
    total: int,
    synced: int,
    error: str | None = None,
) -> None:
    with session_scope(session_factory) as session:
        sync = session.get(Sync, sync_id)
        if sync is None:
            return
        sync.status = status
        sync.total_messages = total
        sync.synced_messages = synced
        sync.finished_at = _utc_naive_now()
        if error is not None:
            sync.error = error


# ---------- worker ----------


async def _process_one_message(
    settings: Settings,
    session_factory: sessionmaker[Session],
    client: GmailClient,
    bucket: TokenBucket,
    sync_id: int,
    message_id: str,
    thread_id: str,
) -> None:
    meta = await _gmail_call_with_retry(
        bucket, client.get_message, message_id, label="messages.get"
    )
    payload = meta.get("payload") or {}

    pending_attachments = await asyncio.to_thread(
        _persist_message_stub,
        session_factory,
        settings,
        message_id,
        thread_id,
        sync_id,
        payload,
    )

    for composite_id, gmail_attachment_id in pending_attachments:
        try:
            content = await _gmail_call_with_retry(
                bucket,
                client.download_attachment,
                message_id,
                gmail_attachment_id,
                label="attachments.get",
            )
            await asyncio.to_thread(
                _persist_attachment_blob,
                session_factory,
                settings,
                composite_id,
                content,
            )
            log.info(
                "attachment.downloaded",
                message_id=message_id,
                composite_id=composite_id,
                bytes=len(content),
            )
        except Exception as e:
            log.exception(
                "attachment.error",
                message_id=message_id,
                composite_id=composite_id,
            )
            await asyncio.to_thread(
                _mark_attachment_error, session_factory, composite_id, str(e)
            )

    await asyncio.to_thread(_mark_message_synced, session_factory, message_id)


async def _worker(
    worker_id: int,
    settings: Settings,
    session_factory: sessionmaker[Session],
    creds,
    bucket: TokenBucket,
    sync_id: int,
    queue: asyncio.Queue,
    counters: dict[str, int],
    on_message_done,
) -> None:
    # Each worker owns its own service object — googleapiclient's
    # http transport isn't strictly thread-safe, and ``to_thread`` may
    # dispatch multiple workers' calls onto the same thread pool worker
    # at different times.
    client = GmailClient(creds)
    while True:
        item = await queue.get()
        try:
            if item is None:
                return
            message_id, thread_id = item
            try:
                await _process_one_message(
                    settings,
                    session_factory,
                    client,
                    bucket,
                    sync_id,
                    message_id,
                    thread_id,
                )
                counters["synced"] += 1
            except Exception as e:
                log.exception(
                    "worker.message_failed",
                    worker_id=worker_id,
                    message_id=message_id,
                )
                counters["failed"] += 1
                await asyncio.to_thread(
                    _mark_message_error, session_factory, message_id, str(e)
                )
            finally:
                if on_message_done is not None:
                    on_message_done(message_id)
        finally:
            queue.task_done()


# ---------- top-level orchestrator ----------


async def run_sync(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    limit: int | None = None,
    since: str | None = None,
    mailbox: MailboxScope = MailboxScope.ALL,
    concurrency: int = 4,
    rate_rps: float = 20.0,
    on_total_known=None,  # called once with the total count to sync
    on_message_done=None,  # called after each message (sync or fail)
) -> int:
    """Run the sync end-to-end. Returns the new ``Sync.id``."""
    creds = load_credentials(settings.token_path)
    enumeration_client = GmailClient(creds)
    bucket = TokenBucket(rate=rate_rps)

    sync_id = await asyncio.to_thread(_create_sync_row, session_factory, mailbox)
    log.info(
        "sync.start",
        sync_id=sync_id,
        limit=limit,
        since=since,
        mailbox=mailbox.value,
        concurrency=concurrency,
        rate_rps=rate_rps,
    )

    query = _build_query(since, mailbox)

    try:
        all_ids = await _enumerate_message_ids(
            bucket, enumeration_client, query, limit
        )
        work = await asyncio.to_thread(
            _filter_needs_work, session_factory, all_ids
        )
        log.info(
            "sync.enumerated",
            total_listed=len(all_ids),
            needs_work=len(work),
            already_done=len(all_ids) - len(work),
        )

        if on_total_known is not None:
            on_total_known(len(work))

        counters = {"synced": 0, "failed": 0}
        if not work:
            await asyncio.to_thread(
                _finalize_sync_row,
                session_factory,
                sync_id,
                status="completed",
                total=len(all_ids),
                synced=0,
            )
            log.info("sync.complete", sync_id=sync_id, synced=0, failed=0)
            return sync_id

        queue: asyncio.Queue = asyncio.Queue(maxsize=max(50, concurrency * 4))
        workers = [
            asyncio.create_task(
                _worker(
                    i,
                    settings,
                    session_factory,
                    creds,
                    bucket,
                    sync_id,
                    queue,
                    counters,
                    on_message_done,
                )
            )
            for i in range(concurrency)
        ]
        for item in work:
            await queue.put(item)
        for _ in range(concurrency):
            await queue.put(None)
        await asyncio.gather(*workers)

        await asyncio.to_thread(
            _finalize_sync_row,
            session_factory,
            sync_id,
            status="completed",
            total=len(all_ids),
            synced=counters["synced"],
        )
        log.info(
            "sync.complete",
            sync_id=sync_id,
            synced=counters["synced"],
            failed=counters["failed"],
        )
        return sync_id
    except Exception as e:
        log.exception("sync.failed", sync_id=sync_id)
        await asyncio.to_thread(
            _finalize_sync_row,
            session_factory,
            sync_id,
            status="failed",
            total=0,
            synced=0,
            error=str(e),
        )
        raise
