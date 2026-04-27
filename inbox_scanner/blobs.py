"""Content-addressed blob storage for downloaded attachments.

Layout: ``<attachments_dir>/blobs/<sha[:2]>/<sha[2:4]>/<sha>``
Two messages with identical attachment bytes share one on-disk blob.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def _shard(sha256_hex: str) -> Path:
    return Path("blobs") / sha256_hex[:2] / sha256_hex[2:4] / sha256_hex


def store_blob(content: bytes, attachments_dir: Path) -> tuple[str, Path]:
    """Write content to its content-addressed location. Idempotent.

    Returns ``(sha256_hex, relative_path)`` where ``relative_path`` is relative
    to ``attachments_dir`` (so it can be stored portably in the DB).
    """
    digest = hashlib.sha256(content).hexdigest()
    rel_path = _shard(digest)
    full_path = attachments_dir / rel_path
    if not full_path.exists():
        full_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = full_path.with_suffix(full_path.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.rename(full_path)
    return digest, rel_path


def read_blob(rel_path: Path | str, attachments_dir: Path) -> bytes:
    return (attachments_dir / Path(rel_path)).read_bytes()


def blob_exists(sha256_hex: str, attachments_dir: Path) -> bool:
    return (attachments_dir / _shard(sha256_hex)).exists()
