"""Hot-store access: Repository pattern over SQLAlchemy 2 Core, plus ``tidal db migrate``.

:class:`SqlRepository` is the only code that touches ``ref.instrument``, ``md.bar_hot``,
``md.tick_hot``, ``ops.migration_watermark`` and ``ops.ingest_checkpoint``. It owns the NaN<->NULL
mapping (data-contracts §3.1) and the versioning rules (§7): a restatement appends ``version+1``
and never overwrites. Statements avoid T-SQL-only syntax so the logic also runs on SQLite in unit
tests; the engine is always injected.
"""

import hashlib
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Annotated, Any, Final

import polars as pl
import pyarrow as pa
import structlog
import typer
from azure.core.credentials import TokenCredential
from sqlalchemy import (
    BigInteger,
    Column,
    Engine,
    Float,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    create_engine,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.engine import URL, Connection

from tidal.common.config import Settings, get_settings
from tidal.common.errors import NotFound, TidalError, ValidationFailed
from tidal.common.timeutil import Clock, SystemClock
from tidal.contracts.schemas import BAR_V1, INTERVAL_CODES, TICK_V1, Interval, QualityFlag

log = structlog.get_logger(__name__)

metadata = MetaData()
_KEY = SmallInteger().with_variant(Integer(), "sqlite")  # sqlite autoincrements INTEGER only


def _table(name: str, schema: str, *cols: Column) -> Table:  # type: ignore[type-arg]
    return Table(name, metadata, *cols, schema=schema)


def _c(name: str, typ: object, **kw: object) -> Column:  # type: ignore[type-arg]
    return Column(name, typ, **kw)  # type: ignore[arg-type]


_S = SmallInteger
instrument = _table(
    "instrument",
    "ref",
    _c("instrument_key", _KEY, primary_key=True, autoincrement=True),
    _c("instrument_id", String(64), nullable=False, unique=True),
    *(
        _c(n, String(w), nullable=False)
        for n, w in [("source", 8), ("asset_class", 16), ("kind", 16)]
    ),
    _c("unit", String(16), nullable=False),
    _c("currency", String(3), nullable=False),
    _c("exchange_tz", String(64), nullable=False),
    _c("granularity", String(4), nullable=False),
    _c("roll_method", String(16)),
    _c("first_ts_ns", BigInteger),
    _c("last_ts_ns", BigInteger),
    _c("description", String(256), nullable=False),
)


def _common_tail() -> tuple[Column, ...]:  # type: ignore[type-arg]
    return (
        _c("quality", _S, nullable=False),
        _c("ingested_at_ns", BigInteger, nullable=False),
        _c("version", _S, nullable=False, primary_key=True),
    )


bar_hot = _table(
    "bar_hot",
    "md",
    _c("instrument_key", _S, primary_key=True),
    _c("interval_code", _S, primary_key=True),
    _c("ts_ns", BigInteger, primary_key=True),
    *(_c(n, Float, nullable=False) for n in ("open", "high", "low", "close")),
    _c("volume", Float),
    _c("trade_count", Integer),
    *_common_tail(),
)
tick_hot = _table(
    "tick_hot",
    "md",
    _c("instrument_key", _S, primary_key=True),
    _c("ts_ns", BigInteger, primary_key=True),
    _c("price", Float, nullable=False),
    _c("size", Float, nullable=False),
    _c("side", _S, nullable=False, primary_key=True),
    *_common_tail(),
)
watermark = _table(
    "migration_watermark",
    "ops",
    _c("table_name", String(128), primary_key=True),
    _c("migrated_until_ns", BigInteger, nullable=False),
    _c("updated_at", String(32), nullable=False),  # DATETIME2(3) in SQL Server; text on SQLite
)
checkpoint = _table(
    "ingest_checkpoint",
    "ops",
    _c("source", String(8), primary_key=True),
    _c("instrument_id", String(64), primary_key=True),
    _c("last_ts_ns", BigInteger, nullable=False),
    _c("updated_at", String(32), nullable=False),
)

_BAR_VALUES: Final = ("open", "high", "low", "close", "volume", "trade_count")
_TICK_VALUES: Final = ("price", "size")
_INSERT_CHUNK: Final = 10_000


@dataclass(frozen=True, slots=True)
class _Kind:
    table: Table
    key: tuple[str, ...]  # versionless primary key
    values: tuple[str, ...]  # columns compared to decide "changed"
    nullable_float: tuple[str, ...]  # NaN <-> NULL columns
    schema: pa.Schema


_BARS = _Kind(bar_hot, ("interval_code", "ts_ns"), _BAR_VALUES, ("volume",), BAR_V1)
_TICKS = _Kind(tick_hot, ("ts_ns", "side"), _TICK_VALUES, (), TICK_V1)


def plan_versions(existing: pl.DataFrame, incoming: pl.DataFrame, kind: _Kind) -> pl.DataFrame:
    """Rows to insert: new keys at version 1, changed values at ``latest+1`` (``RESTATED`` set).

    ``existing`` holds the latest version per key. Unchanged rows are dropped, which makes
    re-ingesting the same data a no-op. NULL compares equal to NULL (``ne_missing``).
    """
    key = list(kind.key)
    if incoming.select(key).is_duplicated().any():
        raise ValidationFailed("duplicate keys within one upsert batch")
    old = existing.select([*key, "version", *kind.values]).rename(
        {c: f"{c}_old" for c in kind.values}
    )
    joined = incoming.join(old, on=key, how="left", suffix="_old")
    changed = pl.any_horizontal(*(pl.col(c).ne_missing(pl.col(f"{c}_old")) for c in kind.values))
    is_new = pl.col("version_old").is_null()
    return (
        joined.filter(is_new | changed)
        .with_columns(
            pl.when(is_new)
            .then(pl.lit(1))
            .otherwise(pl.col("version_old") + 1)
            .cast(pl.Int64)
            .alias("version"),
            pl.when(is_new)
            .then(pl.col("quality"))
            .otherwise(pl.col("quality") | int(QualityFlag.RESTATED))
            .alias("quality"),
        )
        .select(incoming.columns)
    )


class SqlRepository:
    """Repository for the Azure SQL hot store; construct with an injected ``Engine``."""

    def __init__(self, engine: Engine, clock: Clock | None = None) -> None:
        """``clock`` is injectable so ``updated_at`` stamps are deterministic in tests."""
        self.engine = engine
        self._clock: Clock = clock or SystemClock()

    def _stamp(self) -> str:
        return self._clock.now().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    # -- instruments ---------------------------------------------------------------------------
    def upsert_instruments(self, rows: Sequence[Mapping[str, object]]) -> dict[str, int]:
        """Insert unknown instruments, update changed ones; returns ``instrument_id -> key``."""
        with self.engine.begin() as conn:
            have = {r.instrument_id: r for r in conn.execute(select(instrument)).all()}
            for row in rows:
                cur = have.get(str(row["instrument_id"]))
                if cur is None:
                    conn.execute(insert(instrument).values(**row))
                elif any(getattr(cur, k) != v for k, v in row.items()):
                    conn.execute(
                        update(instrument)
                        .where(instrument.c.instrument_key == cur.instrument_key)
                        .values(**row)
                    )
            return self._keys(conn)

    @staticmethod
    def _keys(conn: Connection) -> dict[str, int]:
        q = select(instrument.c.instrument_id, instrument.c.instrument_key)
        return {r.instrument_id: r.instrument_key for r in conn.execute(q)}

    def instrument_keys(self) -> dict[str, int]:
        """Map of every known ``instrument_id`` to its surrogate key."""
        with self.engine.connect() as conn:
            return self._keys(conn)

    # -- bars and ticks ------------------------------------------------------------------------
    def upsert_bars(self, bars: pa.Table) -> int:
        """Apply ``BAR_V1`` rows with version rules (§7); returns the number of rows inserted.

        Instruments must exist: ingest calls :meth:`upsert_instruments` first, else ``NotFound``.
        A changed row always gets ``latest + 1``; the incoming ``version`` is ignored.
        """
        return self._upsert(bars, _BARS)

    def upsert_ticks(self, ticks: pa.Table) -> int:
        """Apply ``TICK_V1`` rows with version rules (§7); returns the number of rows inserted."""
        return self._upsert(ticks, _TICKS)

    def _prepare(self, data: pa.Table, kind: _Kind) -> pl.DataFrame:
        if not data.schema.equals(kind.schema, check_metadata=False):
            raise ValidationFailed("table does not match the contract schema")
        df = pl.from_arrow(data)
        assert isinstance(df, pl.DataFrame)  # noqa: S101 - narrowing for mypy
        keys = self.instrument_keys()
        unknown = set(df["instrument_id"].unique().to_list()) - keys.keys()
        if unknown:
            raise NotFound("unknown instrument in batch")
        df = df.with_columns(
            pl.col("instrument_id").cast(pl.String).replace_strict(keys, return_dtype=pl.Int64)
        ).rename({"instrument_id": "instrument_key"})
        if "interval" in df.columns:
            codes = {str(k): v for k, v in INTERVAL_CODES.items()}
            df = df.with_columns(
                pl.col("interval").cast(pl.String).replace_strict(codes, return_dtype=pl.Int64)
            ).rename({"interval": "interval_code"})
        required = [c for c in kind.values if c not in kind.nullable_float and c != "trade_count"]
        if df.select(pl.any_horizontal(pl.col(required).is_nan())).to_series().any():
            raise ValidationFailed("NaN is not allowed in price columns")
        return df.with_columns(pl.col(list(kind.nullable_float)).fill_nan(None))  # NaN -> NULL

    def _upsert(self, data: pa.Table, kind: _Kind) -> int:
        df = self._prepare(data, kind)
        group = ["instrument_key", *(["interval_code"] if "interval_code" in df.columns else [])]
        inserted = 0
        with self.engine.begin() as conn:
            for part in df.partition_by(group):
                lo, hi = part["ts_ns"].min(), part["ts_ns"].max()
                t = kind.table
                q = select(*t.c).where(t.c.ts_ns.between(lo, hi))
                for g in group:
                    q = q.where(t.c[g] == part[g][0])
                rows = conn.execute(q).mappings().all()
                existing = pl.DataFrame(
                    [dict(r) for r in rows], schema={c.name: _pl_type(c) for c in t.c}
                )
                latest = (
                    existing.sort("version")
                    .group_by(list(dict.fromkeys([*group, *kind.key])))
                    .last()
                )
                todo = plan_versions(latest, part, kind)
                # executemany needs row dicts; this is the DB edge, in bounded chunks.
                for chunk in todo.iter_slices(_INSERT_CHUNK):
                    conn.execute(insert(t), chunk.select([c.name for c in t.c]).to_dicts())
                inserted += todo.height
        return inserted

    def _read(
        self,
        kind: _Kind,
        instrument_id: str,
        interval: Interval | None,
        *,
        start_ns: int,
        end_ns: int,
        as_of_ns: int | None,
    ) -> pa.Table:
        t = kind.table
        key = self.instrument_keys().get(instrument_id)
        if key is None:
            raise NotFound(f"unknown instrument {instrument_id}")
        q = select(*t.c).where(t.c.instrument_key == key, t.c.ts_ns >= start_ns, t.c.ts_ns < end_ns)
        if interval is not None:
            q = q.where(t.c.interval_code == INTERVAL_CODES[interval])
        if as_of_ns is not None:
            q = q.where(t.c.ingested_at_ns <= as_of_ns)
        with self.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(q).mappings()]
        df = pl.DataFrame(rows, schema={c.name: _pl_type(c) for c in t.c})
        df = df.sort([*kind.key, "version"]).group_by(list(kind.key), maintain_order=True).last()
        df = df.sort("ts_ns", *[k for k in kind.key if k != "ts_ns"])
        df = df.with_columns(
            pl.lit(instrument_id).alias("instrument_id"),
            *([pl.lit(str(interval)).alias("interval")] if interval is not None else []),
            *(pl.col(c).fill_null(float("nan")) for c in kind.nullable_float),  # NULL -> NaN
        )
        arrow = df.to_arrow()
        arrays: list[pa.Array[Any]] = []
        for f in kind.schema:
            col = arrow.column(f.name).combine_chunks()
            if pa.types.is_dictionary(f.type):
                col = col.dictionary_encode()
            arrays.append(col.cast(f.type))
        return pa.Table.from_arrays(arrays, schema=kind.schema)

    def read_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        as_of_ns: int | None = None,
    ) -> pa.Table:
        """Latest-version bars with ``start_ns <= ts_ns < end_ns``; ``as_of_ns`` per §7."""
        return self._read(
            _BARS, instrument_id, interval, start_ns=start_ns, end_ns=end_ns, as_of_ns=as_of_ns
        )

    def read_ticks(
        self, instrument_id: str, start_ns: int, end_ns: int, as_of_ns: int | None = None
    ) -> pa.Table:
        """Latest-version ticks with ``start_ns <= ts_ns < end_ns``; ``as_of_ns`` per §7."""
        return self._read(
            _TICKS, instrument_id, None, start_ns=start_ns, end_ns=end_ns, as_of_ns=as_of_ns
        )

    # -- ops tables ----------------------------------------------------------------------------
    def _put(self, table: Table, keys: Mapping[str, object], values: Mapping[str, object]) -> None:
        stamped = {**values, "updated_at": self._stamp()}
        with self.engine.begin() as conn:
            cond = [table.c[k] == v for k, v in keys.items()]
            if conn.execute(update(table).where(*cond).values(**stamped)).rowcount == 0:
                conn.execute(insert(table).values(**keys, **stamped))

    def _get(self, table: Table, col: str, **keys: object) -> int | None:
        q = select(table.c[col]).where(*(table.c[k] == v for k, v in keys.items()))
        with self.engine.connect() as conn:
            row = conn.execute(q).first()
        return None if row is None else int(row[0])

    def set_watermark(self, table_name: str, migrated_until_ns: int) -> None:
        """Set the tiering watermark of ``table_name`` (SPEC-03 §2)."""
        self._put(watermark, {"table_name": table_name}, {"migrated_until_ns": migrated_until_ns})

    def get_watermark(self, table_name: str) -> int | None:
        """Return the watermark, or ``None`` if never set."""
        return self._get(watermark, "migrated_until_ns", table_name=table_name)

    def set_checkpoint(self, source: str, instrument_id: str, last_ts_ns: int) -> None:
        """Record the last ingested timestamp for ``(source, instrument_id)``."""
        self._put(
            checkpoint,
            {"source": source, "instrument_id": instrument_id},
            {"last_ts_ns": last_ts_ns},
        )

    def get_checkpoint(self, source: str, instrument_id: str) -> int | None:
        """Return the checkpoint, or ``None`` if never set."""
        return self._get(checkpoint, "last_ts_ns", source=source, instrument_id=instrument_id)


def _pl_type(col: Column) -> pl.DataType:  # type: ignore[type-arg]
    return pl.Float64() if isinstance(col.type, Float) else pl.Int64()


# -- engine from Settings ----------------------------------------------------------------------
SQL_COPT_SS_ACCESS_TOKEN: Final = 1256
TOKEN_SCOPE: Final = "https://database.windows.net/.default"  # noqa: S105 - a scope, not a secret
_ODBC_DRIVER: Final = "ODBC Driver 18 for SQL Server"


def access_token_struct(token: str) -> bytes:
    """Encode an AAD token the way the ODBC driver expects (``SQL_COPT_SS_ACCESS_TOKEN``)."""
    raw = token.encode("utf-16-le")
    return struct.pack(f"<I{len(raw)}s", len(raw), raw)


def create_engine_from_settings(
    settings: Settings, credential: TokenCredential | None = None
) -> Engine:
    """Build the pyodbc engine for ``settings.sql``.

    ``sql_password``: user/password in the ODBC string (local container, self-signed cert
    trusted only when ``env == local``). ``entra_mi``: no secret; a fresh token from
    ``credential`` is injected through ``attrs_before`` on every new connection.
    """
    s = settings.sql
    parts = [f"DRIVER={{{_ODBC_DRIVER}}}", f"SERVER={s.server}", f"DATABASE={s.database}"]
    if s.auth == "sql_password":
        password = s.password.get_secret_value() if s.password else ""
        parts += [f"UID={s.user}", f"PWD={{{password.replace('}', '}}')}}}"]
    local_container = settings.env == "local" and s.auth == "sql_password"
    parts.append("TrustServerCertificate=yes" if local_container else "Encrypt=yes")
    url = URL.create("mssql+pyodbc", query={"odbc_connect": ";".join(parts)})
    engine = create_engine(url, fast_executemany=True, pool_pre_ping=True)
    if s.auth == "entra_mi":
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415

        cred = credential or DefaultAzureCredential()

        @event.listens_for(engine, "do_connect")
        def _inject_token(
            _dialect: object, _rec: object, _args: object, cparams: dict[str, object]
        ) -> None:
            token = cred.get_token(TOKEN_SCOPE).token
            cparams["attrs_before"] = {SQL_COPT_SS_ACCESS_TOKEN: access_token_struct(token)}

    return engine


# -- migrations --------------------------------------------------------------------------------
class MigrationError(TidalError):
    """A migration file was edited after it was applied, or is badly named."""

    slug = "migration-error"
    title = "Migration error"


@dataclass(frozen=True, slots=True)
class Migration:
    """One ``NNNN_name.sql`` file."""

    version: str
    sql: str
    checksum: str


_BOOTSTRAP: Final = (
    "IF SCHEMA_ID('ops') IS NULL EXEC('CREATE SCHEMA ops')",
    "IF OBJECT_ID('ops.schema_version') IS NULL CREATE TABLE ops.schema_version ("
    "version VARCHAR(128) NOT NULL PRIMARY KEY, checksum CHAR(64) NOT NULL, "
    "applied_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME())",
)


def discover_migrations(directory: Path) -> list[Migration]:
    """Read ``NNNN_*.sql`` files in name order; a missing or empty directory is an error.

    An empty result would otherwise report "nothing to do" against an empty database (seen when
    the container image did not ship ``sql/``).
    """
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    out = []
    for path in sorted(directory.glob("*.sql")):
        if not re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", path.name):
            raise MigrationError(f"bad migration file name {path.name}")
        text = path.read_text(encoding="utf-8")
        out.append(Migration(path.stem, text, hashlib.sha256(text.encode()).hexdigest()))
    if not out:
        raise MigrationError(f"no migration files in {directory}")
    return out


_LOCK_SQL: Final = (
    "DECLARE @rc INT; EXEC @rc = sp_getapplock @Resource = 'tidal:migrate', "
    "@LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 60000; SELECT @rc"
)
_UNLOCK_SQL: Final = "EXEC sp_releaseapplock @Resource = 'tidal:migrate', @LockOwner = 'Session'"


def apply_migrations(engine: Engine, directory: Path) -> list[str]:
    """Apply pending migrations in order, one transaction each; returns the versions applied.

    Idempotent: versions are recorded in ``ops.schema_version``; a changed checksum of an
    applied file raises :class:`MigrationError` instead of silently diverging. On SQL Server the
    whole run holds ``sp_getapplock('tidal:migrate')`` so concurrent runs serialise.
    """
    if engine.dialect.name != "mssql":
        # sp_getapplock is SQL Server only; other dialects (unit-test SQLite) run unlocked.
        return _apply(engine, directory)
    with engine.connect() as lock_conn:  # session-owned lock lives as long as this connection
        rc = lock_conn.exec_driver_sql(_LOCK_SQL).scalar()
        if rc is None or int(rc) < 0:
            raise MigrationError("could not acquire the migration lock (another run active?)")
        try:
            return _apply(engine, directory)
        finally:
            lock_conn.exec_driver_sql(_UNLOCK_SQL)
            lock_conn.commit()


def _apply(engine: Engine, directory: Path) -> list[str]:
    with engine.begin() as conn:
        for stmt in _BOOTSTRAP:
            conn.exec_driver_sql(stmt)
        applied = {
            r[0]: r[1]
            for r in conn.exec_driver_sql("SELECT version, checksum FROM ops.schema_version")
        }
    done = []
    for m in discover_migrations(directory):
        if m.version in applied:
            if applied[m.version].strip() != m.checksum:
                raise MigrationError(f"{m.version} changed after it was applied")
            continue
        with engine.begin() as conn:
            conn.exec_driver_sql(m.sql)
            conn.exec_driver_sql(
                "INSERT INTO ops.schema_version (version, checksum) VALUES (?, ?)",
                (m.version, m.checksum),
            )
        log.info("migration_applied", version=m.version)
        done.append(m.version)
    return done


DEFAULT_MIGRATIONS: Final = Path(__file__).resolve().parents[3] / "sql" / "migrations"
db_app = typer.Typer(name="db", help="Hot-store database commands.", no_args_is_help=True)


@db_app.callback()
def _db() -> None:
    """Hot-store database commands."""


@db_app.command()
def migrate(
    directory: Annotated[
        Path, typer.Option("--dir", help="Folder with NNNN_*.sql.")
    ] = DEFAULT_MIGRATIONS,
) -> None:
    """Apply pending ``sql/migrations/*.sql`` in order (idempotent)."""
    applied = apply_migrations(create_engine_from_settings(get_settings()), directory)
    typer.echo(f"applied: {', '.join(applied) if applied else 'nothing to do'}")
