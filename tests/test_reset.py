"""Tests for the reset helper.

The CLI command itself adds a typer.confirm prompt — we test the inner
``_execute_reset`` function directly so we don't have to mock interactive
input. The path-planning function ``_planned_reset_targets`` is tested
alongside since it's what the confirm preview reads from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inbox_scanner.cli import _execute_reset, _planned_reset_targets
from inbox_scanner.config import load_settings


@pytest.fixture
def populated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tmp data dir with all five canonical artifacts present."""
    monkeypatch.setenv("INBOX_SCANNER__DATA_DIR", str(tmp_path))
    settings = load_settings()
    # `load_settings` already calls `ensure_dirs`, so the skeleton exists.
    # Fill in some content so we can verify what survives a reset.
    (settings.data_dir / "credentials.json").write_text('{"installed": {}}')
    (settings.data_dir / "token.json").write_text('{"refresh_token": "x"}')
    settings.db_path.write_bytes(b"SQLite format 3\x00")
    (settings.attachments_dir / "blobs" / "ab" / "cd").mkdir(parents=True)
    (settings.attachments_dir / "blobs" / "ab" / "cd" / "abcd").write_bytes(b"hello")
    (settings.extracted_dir / "abcd.md").write_text("# extracted text")
    (settings.logs_dir / "scanner.log").write_text("noise")
    return settings


# ---------- _planned_reset_targets ----------


def test_planned_default(populated_data_dir):
    s = populated_data_dir
    targets = _planned_reset_targets(
        s, keep_attachments=False, keep_extractions=False, wipe_all=False
    )
    assert s.db_path in targets
    assert s.logs_dir in targets
    assert s.attachments_dir in targets
    assert s.extracted_dir in targets
    # Token + credentials are NOT in the planned list — they must survive.
    assert (s.data_dir / "token.json") not in targets
    assert (s.data_dir / "credentials.json") not in targets


def test_planned_keep_attachments(populated_data_dir):
    s = populated_data_dir
    targets = _planned_reset_targets(
        s, keep_attachments=True, keep_extractions=False, wipe_all=False
    )
    assert s.attachments_dir not in targets
    assert s.extracted_dir in targets
    assert s.db_path in targets


def test_planned_keep_extractions(populated_data_dir):
    s = populated_data_dir
    targets = _planned_reset_targets(
        s, keep_attachments=False, keep_extractions=True, wipe_all=False
    )
    assert s.extracted_dir not in targets
    assert s.attachments_dir in targets
    assert s.db_path in targets


def test_planned_keep_both(populated_data_dir):
    s = populated_data_dir
    targets = _planned_reset_targets(
        s, keep_attachments=True, keep_extractions=True, wipe_all=False
    )
    assert s.attachments_dir not in targets
    assert s.extracted_dir not in targets
    # Still wipes DB + logs.
    assert s.db_path in targets
    assert s.logs_dir in targets


def test_planned_wipe_all(populated_data_dir):
    s = populated_data_dir
    targets = _planned_reset_targets(
        s, keep_attachments=False, keep_extractions=False, wipe_all=True
    )
    assert targets == [s.data_dir]


# ---------- _execute_reset ----------


def test_default_reset_keeps_oauth(populated_data_dir):
    s = populated_data_dir
    removed = _execute_reset(
        s, keep_attachments=False, keep_extractions=False, wipe_all=False
    )
    # Token + credentials must still be there.
    assert (s.data_dir / "token.json").exists()
    assert (s.data_dir / "credentials.json").exists()
    # State + content gone.
    assert not s.db_path.exists()
    # Skeleton recreated, but contents wiped.
    assert s.attachments_dir.is_dir()
    assert not any(s.attachments_dir.rglob("abcd"))  # the seeded blob
    assert s.extracted_dir.is_dir()
    assert list(s.extracted_dir.iterdir()) == []
    assert s.logs_dir.is_dir()
    assert list(s.logs_dir.iterdir()) == []
    assert removed  # something was actually removed


def test_keep_attachments_preserves_blobs(populated_data_dir):
    s = populated_data_dir
    _execute_reset(
        s, keep_attachments=True, keep_extractions=False, wipe_all=False
    )
    # The seeded blob is still on disk.
    assert (s.attachments_dir / "blobs" / "ab" / "cd" / "abcd").exists()
    # But extracted text and DB are gone.
    assert not s.db_path.exists()
    assert list(s.extracted_dir.iterdir()) == []


def test_keep_extractions_preserves_markdown(populated_data_dir):
    s = populated_data_dir
    _execute_reset(
        s, keep_attachments=False, keep_extractions=True, wipe_all=False
    )
    assert (s.extracted_dir / "abcd.md").exists()
    assert not (s.attachments_dir / "blobs" / "ab" / "cd" / "abcd").exists()


def test_keep_both_only_wipes_db_and_logs(populated_data_dir):
    s = populated_data_dir
    _execute_reset(
        s, keep_attachments=True, keep_extractions=True, wipe_all=False
    )
    assert (s.attachments_dir / "blobs" / "ab" / "cd" / "abcd").exists()
    assert (s.extracted_dir / "abcd.md").exists()
    assert (s.data_dir / "token.json").exists()
    assert (s.data_dir / "credentials.json").exists()
    assert not s.db_path.exists()
    assert list(s.logs_dir.iterdir()) == []


def test_wipe_all_removes_data_dir(populated_data_dir):
    s = populated_data_dir
    removed = _execute_reset(
        s, keep_attachments=False, keep_extractions=False, wipe_all=True
    )
    assert removed == [s.data_dir]
    assert not s.data_dir.exists()


def test_idempotent_on_empty_data_dir(tmp_path: Path, monkeypatch):
    """Running reset on an already-empty data dir must not raise."""
    monkeypatch.setenv("INBOX_SCANNER__DATA_DIR", str(tmp_path))
    settings = load_settings()
    # Clear out the seed dirs that load_settings created.
    import shutil

    shutil.rmtree(settings.data_dir, ignore_errors=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    removed = _execute_reset(
        settings, keep_attachments=False, keep_extractions=False, wipe_all=False
    )
    assert removed == []  # nothing to remove
    # Skeleton still gets recreated.
    assert settings.attachments_dir.is_dir()
    assert settings.logs_dir.is_dir()
