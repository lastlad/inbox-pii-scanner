"""Thin Gmail API wrapper.

Step 2 only needs message listing + metadata fetch (no body bytes). Step 3
will extend this with ``users.messages.attachments.get`` for the actual
attachment downloads, plus a token-bucket rate limiter for the 20 req/sec
budget the plan calls for.

Pure helper functions (header parsing, MIME walking, date parsing) live at
module level so they can be unit-tested without a real Gmail connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


class GmailClient:
    def __init__(self, credentials: Credentials) -> None:
        # cache_discovery=False suppresses the noisy oauth2client cache
        # warning and avoids a stale on-disk discovery doc.
        self._service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )

    def list_message_ids(
        self,
        query: str = "has:attachment",
        max_results: int | None = None,
    ) -> Iterator[tuple[str, str]]:
        """Yield ``(message_id, thread_id)`` tuples matching ``query``.

        Pages through results transparently. Stops after ``max_results`` if
        provided.
        """
        page_token: str | None = None
        yielded = 0
        while True:
            page_size = 500
            if max_results is not None:
                page_size = min(page_size, max_results - yielded)
                if page_size <= 0:
                    return
            resp = (
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    pageToken=page_token,
                    maxResults=page_size,
                )
                .execute()
            )
            for msg in resp.get("messages", []):
                yield msg["id"], msg["threadId"]
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def get_message(self, message_id: str) -> dict[str, Any]:
        """Fetch a message with the full MIME tree.

        Uses ``format=full`` because that's the only format that returns the
        ``parts`` array we need to enumerate attachments — ``format=metadata``
        only returns top-level headers, and ``format=raw`` would force us to
        re-parse MIME ourselves.

        Attachment bytes are *not* inlined here even with ``format=full``: for
        any leaf part with an ``attachmentId`` the response carries the id
        only, and the bytes are fetched separately via
        ``users.messages.attachments.get``. That keeps the response small even
        when the message has a 50 MB PDF attached.

        Quota cost is 5 units (same as ``format=metadata``).
        """
        return (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )


# ---------- pure helpers (no I/O — unit-testable) ----------


def parse_headers(headers: list[dict[str, str]] | None) -> dict[str, str]:
    """Lowercase-key dict of Gmail header entries."""
    if not headers:
        return {}
    return {h["name"].lower(): h["value"] for h in headers}


def parse_received_at(date_header: str | None) -> datetime | None:
    """RFC 2822 ``Date:`` header → naive UTC datetime (for SQLite storage).

    Returns ``None`` if the header is missing or unparseable.
    """
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC; Gmail almost never returns these.
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def walk_attachment_parts(payload: dict[str, Any] | None) -> Iterator[dict[str, Any]]:
    """Recursively yield leaf parts that look like real attachments.

    A "real attachment" is a leaf MIME part with both a non-empty
    ``filename`` and a ``body.attachmentId``. This automatically excludes:

    * Container parts (multipart/*) — they have ``parts`` but no body.
    * Inline body parts without a filename (e.g. inline HTML images
      referenced via Content-ID; the plan's skip rules call these out).
    * Body text/plain and text/html parts (no filename, no attachmentId).
    """
    if not payload:
        return

    parts = payload.get("parts")
    if parts:
        for part in parts:
            yield from walk_attachment_parts(part)
        return

    filename = payload.get("filename") or ""
    body = payload.get("body") or {}
    if filename and body.get("attachmentId"):
        yield payload
