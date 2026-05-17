"""Typer entrypoint for the inbox-scanner CLI."""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Iterator, Optional

import typer
import uvicorn
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from sqlalchemy import func, select

from inbox_scanner.config import Settings, load_settings
from inbox_scanner.db import make_engine, make_session_factory, session_scope
from inbox_scanner.detection.types import Profile
from inbox_scanner.gmail.auth import CredentialsMissing, run_oauth_flow
from inbox_scanner.gmail.sync import MailboxScope, run_sync
from inbox_scanner.logging import configure_logging, get_logger
from inbox_scanner.migrations import apply_migrations
from inbox_scanner.models import Attachment, Detection, Message, MessageVerdict, Scan, Sync
from inbox_scanner.pipelines.scan_pipeline import run_scan
from inbox_scanner.server import create_app

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
    mailbox: Annotated[
        MailboxScope,
        typer.Option(
            case_sensitive=False,
            help=(
                "Which Gmail scope to scan. 'all' (default) matches every "
                "label except spam/trash — inbox + sent + archive. 'inbox' "
                "or 'sent' narrows to that label only. Sensitive documents "
                "you've sent often matter more than what was sent to you."
            ),
        ),
    ] = MailboxScope.ALL,
) -> None:
    """Phase 1: list Gmail messages with attachments, download their bytes,
    and write metadata + attachment stubs to the local DB.

    Idempotent and resumable. Re-runs pick up only the messages that aren't
    fully synced; Ctrl-C is safe at any time. Use ``inbox-scanner status``
    to see what's been captured.
    """
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError as e:
            raise typer.BadParameter(f"--since must be YYYY-MM-DD: {e}") from None
    settings = _bootstrap("sync")
    log = get_logger("cli.sync")
    log.info(
        "sync.invoked",
        limit=limit,
        since=since,
        mailbox=mailbox.value,
    )

    engine = make_engine(settings.db_path)
    session_factory = make_session_factory(engine)

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    task_id = progress.add_task("Syncing", total=None)

    def _on_total_known(total: int) -> None:
        progress.update(task_id, total=total)

    def _on_message_done(_message_id: str) -> None:
        progress.advance(task_id)

    try:
        with _quiet_console_logging(), progress:
            sync_id = asyncio.run(
                run_sync(
                    settings,
                    session_factory,
                    limit=limit,
                    since=since,
                    mailbox=mailbox,
                    on_total_known=_on_total_known,
                    on_message_done=_on_message_done,
                )
            )
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
    profile: Annotated[
        Profile,
        typer.Option(
            case_sensitive=False,
            help=(
                "Detection filter. 'critical' (default) reports only "
                "irreversible-harm entities (SSN, passport, credit card, "
                "IBAN, US bank, ITIN, driver's license, secret, BIP-39 "
                "mnemonic). 'standard' adds account_number and tax_form. "
                "'all' additionally records informational context "
                "(names, addresses, emails, phones, URLs, dates). "
                "Detection still runs all detectors in full — this "
                "filters what gets persisted. Re-scan to switch profiles."
            ),
        ),
    ] = Profile.CRITICAL,
) -> None:
    """Phase 2: extract + detect on locally cached attachments. No Gmail access.

    By default reports only irreversible-harm PII (``--profile critical``).
    Use ``--profile standard`` to also flag broader account-shaped numbers
    and tax-form documents, or ``--profile all`` to also surface
    informational context (names, addresses, emails, etc.).
    """
    if only_extract and only_detect:
        raise typer.BadParameter("--only-extract and --only-detect are mutually exclusive.")
    settings = _bootstrap("scanner")
    log = get_logger("cli.scan")
    log.info(
        "scan.invoked",
        force_extract=force_extract,
        only_extract=only_extract,
        only_detect=only_detect,
        profile=profile.value,
    )

    engine = make_engine(settings.db_path)
    session_factory = make_session_factory(engine)

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    extract_task = progress.add_task("Extracting", total=None, visible=not only_detect)
    detect_task = progress.add_task("Detecting", total=None, visible=not only_extract)

    def _on_extract_total_known(total: int) -> None:
        progress.update(extract_task, total=total)

    def _on_extract_done(_attachment_id: str) -> None:
        progress.advance(extract_task)

    def _on_detect_total_known(total: int) -> None:
        progress.update(detect_task, total=total)

    def _on_detect_done(_attachment_id: str) -> None:
        progress.advance(detect_task)

    with _quiet_console_logging(), progress:
        scan_id = asyncio.run(
            run_scan(
                settings,
                session_factory,
                force_extract=force_extract,
                only_extract=only_extract,
                only_detect=only_detect,
                profile=profile,
                extract_concurrency=settings.extraction.extract_concurrency,
                on_extract_total_known=_on_extract_total_known,
                on_extract_done=_on_extract_done,
                on_detect_total_known=_on_detect_total_known,
                on_detect_done=_on_detect_done,
            )
        )

    console.print(
        f"[green]Scan {scan_id} complete.[/green] Run `inbox-scanner status` for details."
    )


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option(
            help="Bind address. Default 127.0.0.1; overriding will print a loud warning.",
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8765,
) -> None:
    """Start the FastAPI review server.

    The server is read-only and ships no auth — single-user local tool.
    Defaults bind to ``127.0.0.1``; pass ``--host 0.0.0.0`` only if you
    understand that the data dir contains plaintext attachment bytes,
    extracted text, and PII spans.
    """
    settings = _bootstrap("server")
    log = get_logger("cli.serve")
    log.info("serve.invoked", host=host, port=port)

    if host != "127.0.0.1":
        console.print(
            f"[bold red]⚠  Binding to {host}:{port}[/bold red] — this exposes the "
            "scanner's read-only API (and through it, your indexed PII spans) "
            "to anyone reachable on that interface. There is no auth. "
            "Override only if you know what you're doing."
        )

    # Build the FastAPI app once and hand it to uvicorn. We don't use
    # uvicorn's reload mode — that would re-import everything on every
    # save and re-initialize Presidio + Privacy Filter every reload.
    fastapi_app = create_app(settings)
    console.print(
        f"[green]Serving[/green] http://{host}:{port}/  •  "
        f"API docs at http://{host}:{port}/docs  •  Ctrl-C to stop"
    )
    uvicorn.run(
        fastapi_app,
        host=host,
        port=port,
        log_config=None,  # we configure logging ourselves
        access_log=False,
    )


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
        last_scan = session.execute(
            select(Scan).order_by(Scan.started_at.desc()).limit(1)
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

        ext_extracted = session.scalar(
            select(func.count()).select_from(Attachment).where(
                Attachment.extraction_status == "extracted"
            )
        ) or 0
        ext_pending = session.scalar(
            select(func.count()).select_from(Attachment).where(
                Attachment.sync_status == "downloaded",
                Attachment.extraction_status == "pending",
            )
        ) or 0
        ext_unparseable = session.scalar(
            select(func.count()).select_from(Attachment).where(
                Attachment.extraction_status == "unparseable"
            )
        ) or 0
        ext_unscanned = session.scalar(
            select(func.count()).select_from(Attachment).where(
                Attachment.sync_status == "downloaded",
                Attachment.extraction_status.is_(None),
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
    sync_table.add_row("mailbox scope", last_sync.mailbox_scope or "all (legacy row)")
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

    if last_scan is not None:
        scan_table = Table(title="Last scan", show_header=False, box=None)
        scan_table.add_column("k", style="bold")
        scan_table.add_column("v")
        scan_table.add_row("id", str(last_scan.id))
        scan_table.add_row("status", last_scan.status)
        scan_table.add_row("started", str(last_scan.started_at))
        scan_table.add_row("finished", str(last_scan.finished_at))
        scan_table.add_row("attachments processed", str(last_scan.processed_attachments))
        if last_scan.error:
            scan_table.add_row("error", last_scan.error)
        console.print(scan_table)

    with session_scope(session_factory) as session:
        flagged_count = session.scalar(
            select(func.count()).select_from(MessageVerdict).where(
                MessageVerdict.is_flagged.is_(True)
            )
        ) or 0
        verdict_total = session.scalar(
            select(func.count()).select_from(MessageVerdict)
        ) or 0
        category_breakdown = session.execute(
            select(MessageVerdict.top_category, func.count())
            .where(MessageVerdict.is_flagged.is_(True))
            .group_by(MessageVerdict.top_category)
        ).all()
        top_messages = session.execute(
            select(MessageVerdict, Message)
            .join(Message, Message.id == MessageVerdict.message_id)
            .where(MessageVerdict.is_flagged.is_(True))
            .order_by(MessageVerdict.risk_score.desc())
            .limit(5)
        ).all()
        detection_total = session.scalar(
            select(func.count()).select_from(Detection)
        ) or 0

    ext_table = Table(title="Extraction (downloaded attachments only)", show_header=True)
    ext_table.add_column("status")
    ext_table.add_column("count", justify="right")
    ext_table.add_row("extracted", str(ext_extracted))
    ext_table.add_row("pending", str(ext_pending))
    ext_table.add_row("unparseable", str(ext_unparseable))
    ext_table.add_row("not yet scanned", str(ext_unscanned))
    console.print(ext_table)

    if verdict_total == 0:
        console.print(
            "[dim]no detection results yet — run `inbox-scanner scan` to populate findings[/dim]"
        )
        return

    detect_table = Table(title="Detection", show_header=False, box=None)
    detect_table.add_column("k", style="bold")
    detect_table.add_column("v")
    detect_table.add_row("messages with verdict", str(verdict_total))
    detect_table.add_row("flagged", str(flagged_count))
    detect_table.add_row("total findings", str(detection_total))
    console.print(detect_table)

    if category_breakdown:
        cat_table = Table(title="Flagged messages by top category", show_header=True)
        cat_table.add_column("category")
        cat_table.add_column("count", justify="right")
        for cat, n in sorted(category_breakdown, key=lambda kv: -kv[1]):
            cat_table.add_row(cat or "?", str(n))
        console.print(cat_table)

    if top_messages:
        top_table = Table(title="Top 5 flagged by risk score", show_header=True)
        top_table.add_column("risk", justify="right")
        top_table.add_column("category")
        top_table.add_column("from")
        top_table.add_column("subject")
        for verdict, message in top_messages:
            top_table.add_row(
                f"{verdict.risk_score:.0f}",
                verdict.top_category or "?",
                _short(message.sender, 30),
                _short(message.subject, 60),
            )
        console.print(top_table)


def _short(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- reset (still stubbed) ----------


@app.command()
def reset(
    keep_attachments: Annotated[
        bool,
        typer.Option(
            "--keep-attachments",
            help="Preserve downloaded attachment files (skip Gmail re-download on next sync).",
        ),
    ] = False,
    keep_extractions: Annotated[
        bool,
        typer.Option(
            "--keep-extractions",
            help="Preserve extracted text cache (skip Docling re-extraction on next scan).",
        ),
    ] = False,
    wipe_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Also wipe the OAuth token + credentials.json. Forces full re-setup.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Wipe local scanner state.

    By default, keeps your OAuth ``token.json`` and ``credentials.json`` so
    you don't have to redo the Google sign-in or Cloud Console setup; wipes
    everything else (the SQLite DB, downloaded attachments, extracted text,
    logs).

    Use ``--keep-attachments`` to preserve downloads — useful when
    iterating on detector tuning so you don't re-pull everything from
    Gmail. ``--keep-extractions`` additionally preserves the
    Docling-extracted markdown cache. ``--all`` nukes the entire data
    directory, including the OAuth artifacts.
    """
    settings = _bootstrap("cli")
    log = get_logger("cli.reset")
    log.info(
        "reset.invoked",
        keep_attachments=keep_attachments,
        keep_extractions=keep_extractions,
        wipe_all=wipe_all,
    )

    targets = _planned_reset_targets(
        settings,
        keep_attachments=keep_attachments,
        keep_extractions=keep_extractions,
        wipe_all=wipe_all,
    )
    if not targets:
        console.print("[dim]Nothing to remove.[/dim]")
        return

    console.print("[bold]This will delete:[/bold]")
    for p in targets:
        existed = "" if p.exists() else " [dim](does not exist)[/dim]"
        console.print(f"  • {p}{existed}")
    console.print(f"[dim]Data dir: {settings.data_dir}[/dim]")

    if not yes:
        if not typer.confirm("Continue?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(1)

    removed = _execute_reset(
        settings,
        keep_attachments=keep_attachments,
        keep_extractions=keep_extractions,
        wipe_all=wipe_all,
    )
    log.info("reset.done", removed=[str(p) for p in removed], count=len(removed))

    if wipe_all:
        console.print(
            "[green]Wiped everything.[/green] Re-run setup from "
            "'Step 3 — Create a Google OAuth client' in the README."
        )
    else:
        console.print(
            f"[green]Reset complete.[/green] Removed {len(removed)} item(s). "
            "OAuth token preserved — run `inbox-scanner sync` to repopulate."
        )


def _planned_reset_targets(
    settings: Settings,
    *,
    keep_attachments: bool,
    keep_extractions: bool,
    wipe_all: bool,
) -> list[Path]:
    """List of paths the reset *would* operate on (for the confirm preview)."""
    if wipe_all:
        return [settings.data_dir]
    targets: list[Path] = [settings.db_path, settings.logs_dir]
    if not keep_attachments:
        targets.append(settings.attachments_dir)
    if not keep_extractions:
        targets.append(settings.extracted_dir)
    return targets


def _execute_reset(
    settings: Settings,
    *,
    keep_attachments: bool,
    keep_extractions: bool,
    wipe_all: bool,
) -> list[Path]:
    """Perform the wipe. Returns the paths actually removed."""
    targets = _planned_reset_targets(
        settings,
        keep_attachments=keep_attachments,
        keep_extractions=keep_extractions,
        wipe_all=wipe_all,
    )
    removed: list[Path] = []
    for path in targets:
        if path.is_file():
            path.unlink()
            removed.append(path)
        elif path.is_dir():
            shutil.rmtree(path)
            removed.append(path)

    if not wipe_all:
        # Recreate the empty skeleton so the next CLI invocation doesn't
        # need to re-bootstrap from scratch. The DB itself is left absent;
        # _bootstrap → apply_migrations will recreate it on next call.
        settings.ensure_dirs()
    return removed


@contextmanager
def _quiet_console_logging() -> Iterator[None]:
    """Lift the stderr StreamHandler to WARNING for the duration.

    Used during ``sync`` so the rich progress bar isn't fighting per-message
    INFO log lines. The file handler keeps logging at full INFO — every
    sync event is still in ``logs/sync.log``.
    """
    root = logging.getLogger()
    saved: list[tuple[logging.Handler, int]] = []
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            saved.append((h, h.level))
            h.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for h, level in saved:
            h.setLevel(level)
