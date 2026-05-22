"""Content-addressed blob storage roundtrip + idempotence."""

from __future__ import annotations

import hashlib
from pathlib import Path

from inboxaudit.blobs import blob_exists, read_blob, store_blob


def test_store_then_read_roundtrip(tmp_path: Path):
    content = b"the quick brown fox jumps over the lazy dog"
    sha, rel = store_blob(content, tmp_path)
    assert sha == hashlib.sha256(content).hexdigest()
    assert rel == Path("blobs") / sha[:2] / sha[2:4] / sha
    assert (tmp_path / rel).read_bytes() == content
    assert read_blob(rel, tmp_path) == content
    assert blob_exists(sha, tmp_path)


def test_dedup_by_content_hash(tmp_path: Path):
    """Same bytes from two callers produce one on-disk blob."""
    content = b"identical attachment, two emails"
    sha1, rel1 = store_blob(content, tmp_path)
    sha2, rel2 = store_blob(content, tmp_path)
    assert sha1 == sha2
    assert rel1 == rel2
    # Exactly one regular file under the blob shard, no .tmp leftovers.
    files = [p for p in (tmp_path / rel1).parent.iterdir() if p.is_file()]
    assert files == [tmp_path / rel1]


def test_different_content_different_blobs(tmp_path: Path):
    sha_a, rel_a = store_blob(b"a", tmp_path)
    sha_b, rel_b = store_blob(b"b", tmp_path)
    assert sha_a != sha_b
    assert rel_a != rel_b


def test_no_tmp_leftover_after_store(tmp_path: Path):
    """``.tmp`` rename target must be cleaned up — we never want an aborted
    write to leave a file that looks like a real blob."""
    sha, rel = store_blob(b"abc123", tmp_path)
    parent = (tmp_path / rel).parent
    leftovers = [p.name for p in parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_blob_exists_is_false_for_unknown(tmp_path: Path):
    assert not blob_exists("0" * 64, tmp_path)
