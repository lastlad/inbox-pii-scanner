"""Typer entrypoint for the inbox-scanner CLI."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from inbox_scanner.config import Settings, load_settings
from inbox_scanner.db import make_engine, make_session_factory, session_scope
from inbox_scanner.gmail.auth import CredentialsMissing, run_oauth_flow
from inbox_scanner.gmail.sync import run_sync
from inbox_scanner.logging import configure_logging, get_logger
from inbox_scanner.migrations import apply_migrations
from inbox_scanner.models import Attachment, Message, Sync

app = typer.Typer(
    help="Local-first, read-only Gmail PII scanner.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _bootstrap(phase: str) -> Settings:
    settings = load_settings()
    configure_logging(settings.logs_dir, phase=phase)  # type: ignore[arg-type]
    apply_migrations(settings)
    return settings


@app.callback()
def _root() -> None:
    """Top-level options can hang here later (e.g. --data-dir)."""


# ---------- auth ----------


@app.command()
def auth() -> None:
    """Walk through the Google OAuth flow and save token.json."""
    settings = _bootstrap("cli")
    log = get_logger("cli.auth")
    log.info("auth.invoked", credentials=str(settings.credentials_path))
    try:
        creds = run_oauth_flow(settings.credentials_path, settings.token_path)
    except CredentialsMissing as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]Authenticated.[/green] Token saved to {settings.token_path}")
    console.print(f"Scopes: {', '.join(creds.scopes or [])}")


# ---------- sync ----------


@app.command()
def sync(
    limit: Annotated[
        Optional[int], typer.Option(help="Max messages to process this run.")
    ] = None,
    since: Annotated[
        Optional[str],
        typer.Option(help="Only fetch messages on or after this date (YYYY-MM-DD)."),
    ] = None,
    resume: Annotated[
        bool, typer.Option(help="Resume an interrupted sync (idempotent default).")
    ] = True,
) -> None:
    """Phase 1: list Gmail messages with attachments and write metadata stubs.

    Step-2 scope: does **not** download attachment bytes yet — that lands in
    step 3. Use ``inbox-scanner status`` to see what was captured.
    """
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError as e:
            raise typer.BadParameter(f"--since must be YYYY-MM-DD: {e}") from None
    settings = _bootstrap("sync")
    log = get_logger("cli.sync")
    log.info("sync.invoked", limit=limit, since=since, resume=resume)

    engine = make_engine(settings.db_path)
    session_factory = make_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            sync_id = run_sync(settings, session, limit=limit, since=since)
    except CredentialsMissing as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    console.print(f"[green]Sync {sync_id} complete.[/green] Run `inbox-scanner status` for details.")


# ---------- scan (still stubbed; arrives in steps 4-7) ----------


@app.command()
def scan(
    force_extract: Annotated[
        bool,
        typer.Option(
            "--force-extract", help="Re-run extraction even on cached attachments."
        ),
    ] = False,
    only_extract: Annotated[
        bool,
        typer.Option("--only-extract", help="Run extraction only; skip detection."),
    ] = False,
    only_detect: Annotated[
        bool,
        typer.Option("--only-detect", help="Run detection only; skip extraction."),
    ] = False,
) -> None:
    """Phase 2: extract + detect on locally cached attachments. No Gmail access."""
    if only_extract and only_detect:
        raise typer.BadParameter("--only-extract and --only-detect are mutually exclusive.")
    _bootstrap("scanner")
    log = get_logger("cli.scan")
    log.info(
        "scan.invoked",
        force_extract=force_extract,
        only_extract=only_extract,
        only_detect=only_detect,
    )
    raise typer.Exit(_not_implemented("scan"))


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port to bind on 127.0.0.1.")] = 8765,
) -> None:
    """Start the FastAPI review server (localhost only)."""
    _bootstrap("server")
    log = get_logger("cli.serve")
    log.info("serve.invoked", port=port)
    raise typer.Exit(_not_implemented("serve"))


# ---------- status ----------


@app.command()
def status() -> None:
    """Print sync state + attachment cache summary."""
    settings = _bootstrap("cli")
    engine = make_engine(settings.db_path)
    session_factory = make_session_factory(engine)

    with session_scope(session_factory) as session:
        last_sync = session.execute(
            select(Sync).order_by(Sync.started_at.desc()).limit(1)
        ).scalar_one_or_none()

        msg_total = session.scalar(select(func.count()).select_from(Message)) or 0
        msg_synced = session.scalar(
            select(func.count()).select_from(Message).where(Message.sync_status == "synced")
        ) or 0
        msg_pending = session.scalar(
            select(func.count()).select_from(Message).where(Message.sync_status == "pending")
        ) or 0
        msg_error = session.scalar(
            select(func.count()).select_from(Message).where(Message.sync_status == "sync_error")
        ) or 0

        att_total = session.scalar(select(func.count()).select_from(Attachment)) or 0
        att_pending = session.scalar(
            select(func.count()).select_from(Attachment).where(Attachment.sync_status == "pending")
        ) or 0
        att_downloaded = session.scalar(
            select(func.count()).select_from(Attachment).where(Attachment.sync_status == "downloaded")
        ) or 0
        att_skipped = session.scalar(
            select(func.count()).select_from(Attachment).where(
                Attachment.sync_status.in_(("skipped_filter", "skipped_too_large"))
            )
        ) or 0

    console.print(f"[bold]data_dir[/bold]: {settings.data_dir}")
    console.print(f"[bold]db[/bold]:       {settings.db_path}")

    if last_sync is None:
        console.print("[dim]no syncs yet — run `inbox-scanner auth` then `inbox-scanner sync --limit 5`[/dim]")
        return

    sync_table = Table(title="Last sync", show_header=False, box=None)
    sync_table.add_column("k", style="bold")
    sync_table.add_column("v")
    sync_table.add_row("id", str(last_sync.id))
    sync_table.add_row("status", last_sync.status)
    sync_table.add_row("started", str(last_sync.started_at))
    sync_table.add_row("finished", str(last_sync.finished_at))
    sync_table.add_row("messages seen", str(last_sync.total_messages))
    sync_table.add_row("messages synced", str(last_sync.synced_messages))
    if last_sync.error:
        sync_table.add_row("error", last_sync.error)
    console.print(sync_table)

    msg_table = Table(title="Messages", show_header=True)
    msg_table.add_column("status")
    msg_table.add_column("count", justify="right")
    msg_table.add_row("synced", str(msg_synced))
    msg_table.add_row("pending", str(msg_pending))
    msg_table.add_row("sync_error", str(msg_error))
    msg_table.add_row("[bold]total[/bold]", f"[bold]{msg_total}[/bold]")
    console.print(msg_table)

    att_table = Table(title="Attachments", show_header=True)
    att_table.add_column("status")
    att_table.add_column("count", justify="right")
    att_table.add_row("downloaded", str(att_downloaded))
    att_table.add_row("pending", str(att_pending))
    att_table.add_row("skipped", str(att_skipped))
    att_table.add_row("[bold]total[/bold]", f"[bold]{att_total}[/bold]")
    console.print(att_table)


# ---------- reset (still stubbed) ----------


@app.command()
def reset(
    keep_token: Annotated[bool, typer.Option("--keep-token", help="Keep the OAuth token.")] = False,
    keep_attachments: Annotated[
        bool,
        typer.Option("--keep-attachments", help="Keep downloaded attachment blobs."),
    ] = False,
    keep_extractions: Annotated[
        bool,
        typer.Option("--keep-extractions", help="Keep cached extracted text."),
    ] = False,
) -> None:
    """Wipe local state. By default keeps nothing unless --keep-* flags are passed."""
    _bootstrap("cli")
    log = get_logger("cli.reset")
    log.info(
        "reset.invoked",
        keep_token=keep_token,
        keep_attachments=keep_attachments,
        keep_extractions=keep_extractions,
    )
    raise typer.Exit(_not_implemented("reset"))


def _not_implemented(name: str) -> int:
    console.print(
        f"[yellow]`inbox-scanner {name}` is not implemented yet.[/yellow] "
        "See docs/IMPLEMENTATION_PLAN.md for build status."
    )
    return 1
