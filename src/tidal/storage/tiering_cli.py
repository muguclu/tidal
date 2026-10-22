"""``tidal tiering run | retag | compact`` (SPEC-03 §2-3.1); wiring only, logic is in the modules.

Exit codes: 0 done (also "another run holds the lock"), 1 data errors (the job is safe to re-run).
"""

from typing import Annotated, Final

import typer
from sqlalchemy import Engine

from tidal.common.config import Settings, get_settings
from tidal.common.errors import TidalError
from tidal.common.logging import configure_logging, correlation_scope, get_logger
from tidal.common.timeutil import Clock, clock_from_settings
from tidal.storage import layout
from tidal.storage import sql_repo as sr
from tidal.storage.compact import compact
from tidal.storage.parquet_writer import AzureBlobStore, LocalStore, ObjectStore
from tidal.storage.retag import AzureTagger, BlobTagger, LocalTagger, retag
from tidal.storage.tiering import (
    DEFAULT_BATCH_ROWS,
    DEFAULT_DELETE_CHUNK_ROWS,
    TieringConfig,
    run_tiering,
    spec_for_table,
)

_log = get_logger(__name__)
_DEFAULT_TABLES: Final = ("bar_hot", "tick_hot")

app = typer.Typer(
    name="tiering",
    help="Hot -> cold tiering: migrate old rows to Parquet, re-tag by age, compact partitions.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def _build(settings: Settings) -> tuple[ObjectStore, BlobTagger]:
    """Store and tagger for the configured backend (reuses the ingest store builder)."""
    from tidal.ingest.cli import build_store  # noqa: PLC0415 - heavy import, only for the CLI

    store = build_store(settings)
    if isinstance(store, AzureBlobStore):
        return store, AzureTagger(store.service)
    if isinstance(store, LocalStore):
        return store, LocalTagger(store.root)
    raise TypeError("unsupported object store")  # pragma: no cover


def _context() -> tuple[Settings, Engine, ObjectStore, BlobTagger, Clock]:
    settings = get_settings()
    configure_logging(settings)
    store, tagger = _build(settings)
    return (
        settings,
        sr.create_engine_from_settings(settings),
        store,
        tagger,
        clock_from_settings(settings),
    )


def _fail(exc: TidalError) -> typer.Exit:
    _log.error("tiering_failed", error=exc.slug, detail=str(exc))
    return typer.Exit(1)


@app.command("run")
def run_cmd(
    table: Annotated[
        list[str] | None,
        typer.Option(help="bar_hot and/or tick_hot, or md.bar_hot / md.tick_hot (default: both)"),
    ] = None,
    hot_window_days: Annotated[
        int | None, typer.Option(help="Override TIDAL_HOT_WINDOW_DAYS")
    ] = None,
    batch_rows: Annotated[int, typer.Option(help="Rows per SQL read page")] = DEFAULT_BATCH_ROWS,
    delete_chunk_rows: Annotated[
        int, typer.Option(help="Rows per delete transaction")
    ] = DEFAULT_DELETE_CHUNK_ROWS,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan only: report what would move, change nothing")
    ] = False,
) -> None:
    """Move rows older than the hot window from SQL to Parquet (idempotent, resumable)."""
    names = tuple(table or _DEFAULT_TABLES)
    try:
        for name in names:
            spec_for_table(name)
    except layout.InvalidPath:
        raise typer.BadParameter(f"unknown table in {names}; use bar_hot or tick_hot") from None
    with correlation_scope():
        try:
            settings, engine, store, tagger, clock = _context()
            cfg = TieringConfig(
                hot_window_days or settings.hot_window_days, batch_rows, delete_chunk_rows, dry_run
            )
            results = run_tiering(engine, store, tagger, clock, cfg, names)
        except TidalError as exc:
            raise _fail(exc) from exc
    for r in results:
        typer.echo(
            f"{r.table}: moved={r.rows_moved} deduped={r.rows_deduped} swept={r.rows_swept} "
            f"deleted={r.rows_deleted} bytes={r.bytes_written} locked={r.locked} "
            f"dry_run={r.dry_run} errors={len(r.errors)}"
        )
    if any(r.errors for r in results):
        raise typer.Exit(1)


@app.command("retag")
def retag_cmd(
    dataset: Annotated[
        list[str] | None, typer.Option(help="Dataset(s) to re-tag (default: all)")
    ] = None,
) -> None:
    """Recompute ``age_band`` for all blobs (run yearly, Jan 2)."""
    with correlation_scope():
        try:
            _, engine, store, tagger, clock = _context()
            res = retag(store, tagger, clock, engine, datasets=tuple(dataset or layout.DATASETS))
        except TidalError as exc:
            raise _fail(exc) from exc
    typer.echo(
        f"retag: blobs={res.blobs} changed={res.changed} missing={res.missing} "
        f"skipped_datasets={res.skipped_datasets}"
    )


@app.command("compact")
def compact_cmd(
    dataset: Annotated[str, typer.Option(help="Dataset to compact, e.g. bars_1d")],
    instrument: Annotated[str | None, typer.Option(help="Only this instrument_id")] = None,
    year: Annotated[int | None, typer.Option(help="Only this partition year")] = None,
) -> None:
    """Merge the parts of each partition into one file, keeping every version."""
    with correlation_scope():
        try:
            _, engine, store, tagger, clock = _context()
            res = compact(
                engine, store, tagger, clock, dataset, instrument_id=instrument, year=year
            )
        except TidalError as exc:
            raise _fail(exc) from exc
    typer.echo(
        f"compact {dataset}: partitions={res.partitions} merged={res.merged} "
        f"written={res.parts_written} removed={res.parts_removed} locked={res.locked} "
        f"errors={len(res.errors)}"
    )
    if res.errors:
        raise typer.Exit(1)
