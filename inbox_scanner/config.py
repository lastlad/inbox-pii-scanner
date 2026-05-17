from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Documented end-user default. Used when no source checkout is detected and
#: no explicit override is provided.
USER_DATA_DIR = Path.home() / ".inbox-scanner"

#: Repo-local data dir name used during development.
DEV_DATA_DIRNAME = ".inbox-scanner-data"

CONFIG_FILENAME = "config.yaml"
ENV_DATA_DIR_VAR = "INBOX_SCANNER__DATA_DIR"


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: cwd) looking for ``pyproject.toml``.

    Returns the directory containing it, or ``None`` if none is found before
    the filesystem root. Used both to (a) decide whether we're in a source
    checkout (so dev runs can default to a repo-local data dir) and (b)
    resolve relative ``data_dir`` overrides from any cwd inside the repo.
    """
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def default_data_dir() -> Path:
    """Pick the data directory to use when no override is provided.

    Inside a source checkout (a ``pyproject.toml`` is reachable upward from
    cwd), prefer ``<repo>/.inbox-scanner-data/`` so dev state stays inside
    the repo and is easy to inspect / reset / nuke. End users running an
    installed wheel won't have a ``pyproject.toml`` upward, so they fall
    through to the documented ``~/.inbox-scanner`` default.

    This means we ship **no** committed config file, no ``.env``, and no
    "copy this template" step — running the CLI from a clean clone Just
    Works and lands all state inside the repo.
    """
    root = find_project_root()
    if root is not None:
        return root / DEV_DATA_DIRNAME
    return USER_DATA_DIR


class GmailConfig(BaseModel):
    credentials_path: Path | None = None
    token_path: Path | None = None
    rate_limit_rps: int = 20
    max_total_bytes: int = 100 * 1024 * 1024 * 1024  # 100 GB safety cap


class ExtractionConfig(BaseModel):
    max_attachment_bytes: int = 50 * 1024 * 1024  # 50 MB
    # Docling does its own internal concurrency for layout/OCR. We add an
    # outer thread-pool semaphore in scan_pipeline mostly to keep the
    # progress bar honest and avoid spawning more workers than CPU cores.
    extract_concurrency: int = 2


class DetectionConfig(BaseModel):
    presidio_threshold: float = 0.5
    privacy_filter_threshold: float = 0.6


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class Settings(BaseSettings):
    """Top-level configuration.

    Loaded by :func:`load_settings`, which merges:

    1. Defaults defined here.
    2. ``<data_dir>/config.yaml`` if present (user-editable, gitignored —
       this is where API keys / OAuth client paths / detector thresholds
       belong).
    3. Environment variables prefixed ``INBOX_SCANNER__``.

    No ``.env`` support. Secrets and per-environment overrides go in
    ``config.yaml`` inside the data directory (which is gitignored), or in
    real environment variables — never in a tracked dotfile.
    """

    model_config = SettingsConfigDict(
        env_prefix="INBOX_SCANNER__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=default_data_dir)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def extracted_dir(self) -> Path:
        return self.data_dir / "extracted"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def credentials_path(self) -> Path:
        return self.gmail.credentials_path or (self.data_dir / "credentials.json")

    @property
    def token_path(self) -> Path:
        return self.gmail.token_path or (self.data_dir / "token.json")

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.attachments_dir,
            self.attachments_dir / "blobs",
            self.extracted_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _resolve_data_dir(raw: Path) -> Path:
    if raw.is_absolute():
        return raw
    root = find_project_root() or Path.cwd()
    return (root / raw).resolve()


def load_settings(data_dir: Path | None = None) -> Settings:
    """Load settings, merging YAML + env, and ensure the data directory exists.

    Resolution order for ``data_dir``:

    1. Explicit ``data_dir`` argument.
    2. ``INBOX_SCANNER__DATA_DIR`` environment variable.
    3. :func:`default_data_dir` — repo-local in a source checkout, else
       ``~/.inbox-scanner``.

    Relative paths in (1) and (2) resolve against the project root (the
    directory containing ``pyproject.toml``), so a shell-exported override
    behaves the same regardless of cwd.
    """
    if data_dir is not None:
        resolved = _resolve_data_dir(data_dir)
    elif (env_override := os.environ.get(ENV_DATA_DIR_VAR)):
        resolved = _resolve_data_dir(Path(env_override))
    else:
        resolved = default_data_dir().resolve()

    config_path = resolved / CONFIG_FILENAME
    raw: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open() as f:
            raw = yaml.safe_load(f) or {}

    # Always pin data_dir to the resolved absolute path so downstream code
    # doesn't depend on cwd or the env variable still being set.
    raw["data_dir"] = str(resolved)

    settings = Settings(**raw)
    settings.ensure_dirs()
    return settings
