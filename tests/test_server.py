"""Tests for the FastAPI server.

Each test gets its own isolated data dir + SQLite DB so we can seed
canned messages/attachments/detections without touching the dev corpus.
TestClient is FastAPI's in-process client — no real network sockets.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from inbox_scanner.config import load_settings
from inbox_scanner.db import session_scope
from inbox_scanner.migrations import apply_migrations
from inbox_scanner.models import (
    Attachment,
    Detection,
    Message,
    MessageVerdict,
    Scan,
    Sync,
)
from inbox_scanner.server import create_app


@pytest.fixture
def fresh_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``load_settings`` at an empty tmpdir and run migrations there."""
    monkeypatch.setenv("INBOX_SCANNER__DATA_DIR", str(tmp_path))
    settings = load_settings()
    apply_migrations(settings)
    return tmp_path


@pytest.fixture
def session_factory(fresh_data_dir: Path):
    settings = load_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def client(fresh_data_dir: Path):
    settings = load_settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _seed_basic_corpus(sf, settings, with_findings: bool = True) -> None:
    """Two messages, two attachments, one extracted text, optional detections."""
    extracted_dir = settings.extracted_dir
    extracted_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "Receipt for John Doe.\n"
        "Account number 4111-1111-1111-1111 was charged $42.99.\n"
        "Address: 1 Infinite Loop, Cupertino CA.\n"
    )
    md_path = extracted_dir / "abc123.md"
    md_path.write_text(text, encoding="utf-8")

    with session_scope(sf) as session:
        sync = Sync(
            started_at=datetime(2026, 5, 7, 12, 0, 0),
            finished_at=datetime(2026, 5, 7, 12, 5, 0),
            status="completed",
            total_messages=2,
            synced_messages=2,
        )
        scan = Scan(
            started_at=datetime(2026, 5, 7, 13, 0, 0),
            finished_at=datetime(2026, 5, 7, 13, 1, 0),
            status="completed",
            total_attachments=2,
            processed_attachments=2,
        )
        session.add_all([sync, scan])
        session.flush()

        m_high = Message(
            id="msg_high",
            thread_id="t1",
            sync_id=sync.id,
            sender='"Receipts Inc" <noreply@receipts.example>',
            subject="Your receipt #4242",
            received_at=datetime(2026, 5, 6, 10, 0, 0),
            has_attachments=True,
            attachment_count=1,
            sync_status="synced",
        )
        m_low = Message(
            id="msg_low",
            thread_id="t2",
            sync_id=sync.id,
            sender='"Newsletter" <news@example.org>',
            subject="Weekly digest",
            received_at=datetime(2026, 5, 6, 11, 0, 0),
            has_attachments=True,
            attachment_count=1,
            sync_status="synced",
        )
        session.add_all([m_high, m_low])

        a_high = Attachment(
            id="msg_high:0",
            message_id="msg_high",
            filename="receipt.pdf",
            mime_type="application/pdf",
            size_bytes=12345,
            content_hash="abc123",
            blob_path="blobs/ab/c1/abc123",
            sync_status="downloaded",
            extraction_status="extracted",
            extraction_route="docling",
            extracted_text_path="abc123.md",
            extracted_at=datetime(2026, 5, 7, 13, 0, 30),
        )
        a_low = Attachment(
            id="msg_low:0",
            message_id="msg_low",
            filename="header.png",
            mime_type="image/png",
            size_bytes=2048,
            content_hash="def456",
            blob_path="blobs/de/f4/def456",
            sync_status="downloaded",
            extraction_status="extracted",
            extraction_route="docling",
            extracted_text_path="def456.md",
            extracted_at=datetime(2026, 5, 7, 13, 0, 45),
        )
        session.add_all([a_high, a_low])

        if with_findings:
            session.add_all(
                [
                    # High-risk: a CREDIT_CARD finding
                    Detection(
                        scan_id=scan.id,
                        attachment_id="msg_high:0",
                        category="financial",
                        subtype="CREDIT_CARD",
                        detector="presidio",
                        span_text="4111-1111-1111-1111",
                        span_start=text.index("4111"),
                        span_end=text.index("4111") + len("4111-1111-1111-1111"),
                        confidence=1.0,
                        created_at=datetime(2026, 5, 7, 13, 0, 50),
                    ),
                    # Low-risk: only an other_pii name finding
                    Detection(
                        scan_id=scan.id,
                        attachment_id="msg_low:0",
                        category="other_pii",
                        subtype="private_person",
                        detector="privacy_filter",
                        span_text="Editor in Chief",
                        span_start=0,
                        span_end=15,
                        confidence=0.9,
                        created_at=datetime(2026, 5, 7, 13, 0, 55),
                    ),
                ]
            )
            session.add_all(
                [
                    MessageVerdict(
                        message_id="msg_high",
                        scan_id=scan.id,
                        is_flagged=True,
                        top_category="financial",
                        risk_score=7.0,
                        category_summary={"financial": 1},
                    ),
                    MessageVerdict(
                        message_id="msg_low",
                        scan_id=scan.id,
                        is_flagged=False,
                        top_category="other_pii",
                        risk_score=0.0,
                        category_summary={"other_pii": 1},
                    ),
                ]
            )


# ---------- /api/stats ----------


def test_stats_empty_db(client: TestClient):
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["sync"]["total_messages"] == 0
    assert body["sync"]["last_sync_at"] is None
    assert body["scan"]["total_flagged"] == 0
    assert body["scan"]["by_top_category"] == {}


def test_stats_populated(client: TestClient, session_factory):
    settings = load_settings()
    _seed_basic_corpus(session_factory, settings)
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["sync"]["total_messages"] == 2
    assert body["sync"]["total_attachments"] == 2
    assert body["sync"]["total_attachments_downloaded"] == 2
    assert body["sync"]["last_sync_status"] == "completed"
    assert body["scan"]["total_flagged"] == 1
    assert body["scan"]["total_findings"] == 2
    assert body["scan"]["by_top_category"] == {"financial": 1}


# ---------- /api/flagged ----------


def test_flagged_only_returns_flagged(client: TestClient, session_factory):
    settings = load_settings()
    _seed_basic_corpus(session_factory, settings)
    r = client.get("/api/flagged")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["message_id"] == "msg_high"
    assert item["is_flagged"] is True
    assert item["top_category"] == "financial"
    assert item["risk_score"] == 7.0
    assert item["category_summary"] == {"financial": 1}
    assert item["gmail_url"].endswith("/msg_high")


def test_flagged_filter_by_category(client: TestClient, session_factory):
    settings = load_settings()
    _seed_basic_corpus(session_factory, settings)
    r = client.get("/api/flagged?category=financial")
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = client.get("/api/flagged?category=tax")
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["items"] == []


def test_flagged_pagination(client: TestClient, session_factory):
    settings = load_settings()
    _seed_basic_corpus(session_factory, settings)
    r = client.get("/api/flagged?limit=1&cursor=0")
    body = r.json()
    assert len(body["items"]) == 1
    # Only one flagged message exists, so next_cursor should be None.
    assert body["next_cursor"] is None


def test_flagged_sort_validates_enum(client: TestClient):
    r = client.get("/api/flagged?sort=alphabetical")
    assert r.status_code == 422


# ---------- /api/email/{id} ----------


def test_email_detail_404(client: TestClient):
    r = client.get("/api/email/does-not-exist")
    assert r.status_code == 404


def test_email_detail_payload(client: TestClient, session_factory):
    settings = load_settings()
    _seed_basic_corpus(session_factory, settings)
    r = client.get("/api/email/msg_high")
    assert r.status_code == 200
    body = r.json()
    assert body["message_id"] == "msg_high"
    assert body["is_flagged"] is True
    assert body["top_category"] == "financial"
    assert body["gmail_url"].endswith("/msg_high")

    assert len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["filename"] == "receipt.pdf"
    assert att["extraction_status"] == "extracted"
    assert len(att["findings"]) == 1
    f = att["findings"][0]
    assert f["category"] == "financial"
    assert f["span_text"] == "4111-1111-1111-1111"
    # Snippet should contain the credit card and surrounding context.
    assert "4111-1111-1111-1111" in f["snippet"]
    # Snippet-relative offsets should re-find the span inside the snippet.
    assert (
        f["snippet"][f["snippet_relative_start"] : f["snippet_relative_end"]]
        == "4111-1111-1111-1111"
    )


def test_email_detail_snippet_window_param(client: TestClient, session_factory):
    settings = load_settings()
    _seed_basic_corpus(session_factory, settings)
    r = client.get("/api/email/msg_high?snippet_window=5")
    body = r.json()
    f = body["attachments"][0]["findings"][0]
    # With a 5-char window, the snippet is the span itself (~19 chars) +
    # ≤5 chars on each side, so well under 30.
    assert len(f["snippet"]) <= 30
    assert "4111-1111-1111-1111" in f["snippet"]


# ---------- / (placeholder index) ----------


def test_index_returns_html(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # The placeholder advertises the API; it's HTML and mentions /api/stats.
    assert "/api/stats" in r.text
