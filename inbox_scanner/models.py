from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Sync(Base):
    __tablename__ = "syncs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32))  # running | completed | failed
    total_messages: Mapped[int | None] = mapped_column(Integer)
    synced_messages: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32))
    total_attachments: Mapped[int | None] = mapped_column(Integer)
    processed_attachments: Mapped[int | None] = mapped_column(Integer)
    config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Gmail message ID
    thread_id: Mapped[str | None] = mapped_column(String)
    sync_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("syncs.id"))
    sender: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)
    has_attachments: Mapped[bool | None] = mapped_column(Boolean)
    attachment_count: Mapped[int | None] = mapped_column(Integer)
    sync_status: Mapped[str] = mapped_column(String(32), default="pending")
    sync_error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message")

    __table_args__ = (Index("idx_messages_sync_status", "sync_status"),)


class Attachment(Base):
    __tablename__ = "attachments"

    # Composite of (message_id, part_id). part_id is Gmail's *stable* MIME
    # part identifier; gmail_attachment_id below is the volatile handle used
    # for the actual download call and gets refreshed on every metadata
    # fetch.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(String, ForeignKey("messages.id"))
    part_id: Mapped[str | None] = mapped_column(String(64))
    gmail_attachment_id: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 hex
    blob_path: Mapped[str | None] = mapped_column(Text)
    sync_status: Mapped[str] = mapped_column(String(32), default="pending")
    sync_error: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)

    last_scan_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("scans.id"))
    extraction_route: Mapped[str | None] = mapped_column(String(32))  # docling | qwen-vl | unparseable
    extraction_status: Mapped[str | None] = mapped_column(String(32))  # extracted | unparseable | pending
    extracted_text_path: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)
    extraction_error: Mapped[str | None] = mapped_column(Text)

    message: Mapped[Message] = relationship(back_populates="attachments")

    __table_args__ = (
        Index("idx_attachments_message", "message_id"),
        Index("idx_attachments_extraction", "extraction_status"),
        Index("idx_attachments_hash", "content_hash"),
    )


class Detection(Base):
    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(Integer, ForeignKey("scans.id"))
    attachment_id: Mapped[str] = mapped_column(String, ForeignKey("attachments.id"))
    category: Mapped[str] = mapped_column(String(32))
    subtype: Mapped[str | None] = mapped_column(String(64))
    detector: Mapped[str] = mapped_column(String(32))
    span_text: Mapped[str | None] = mapped_column(Text)
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("idx_detections_scan", "scan_id"),
        Index("idx_detections_attachment", "attachment_id"),
    )


class MessageVerdict(Base):
    __tablename__ = "message_verdicts"

    message_id: Mapped[str] = mapped_column(String, ForeignKey("messages.id"), primary_key=True)
    scan_id: Mapped[int] = mapped_column(Integer, ForeignKey("scans.id"))
    is_flagged: Mapped[bool] = mapped_column(Boolean)
    top_category: Mapped[str | None] = mapped_column(String(32))
    risk_score: Mapped[float | None] = mapped_column(Float)
    category_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (Index("idx_verdicts_flagged", "is_flagged", "risk_score"),)
