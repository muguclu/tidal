"""DuckDB session factory for cold Parquet reads (SPEC-03 §5).

Backends
- ``LocalStore`` root: plain filesystem paths, no extension needed.
- Azurite / Azure: DuckDB's ``azure`` extension and ``az://container/blob`` URIs. Locally the
  storage connection string is bound as a parameter (``SET GLOBAL
  azure_storage_connection_string = ?``; global because per-query cursors do not inherit session
  settings). With ``use_managed_identity`` a ``credential_chain`` secret (Managed Identity first)
  is created.

Hardening: after setup every session runs ``SET autoinstall_known_extensions=false``,
``autoload_known_extensions=false`` and ``lock_configuration=true``. Outside ``TIDAL_ENV=local``
only ``LOAD azure`` runs (no ``INSTALL``, no network fetch): the T-305 image must bake
``azure.duckdb_extension`` into a fixed ``extension_directory`` (``~/.duckdb/extensions/...`` of the
image user, or one set via the ``extension_directory`` option) at build time.

Extension install (local only): ``INSTALL azure`` downloads ``azure.duckdb_extension`` (~5 s) into
``~/.duckdb/extensions/<duckdb version>/<platform>/`` on first use and needs outbound HTTPS to
``extensions.duckdb.org``; later runs load it from that cache, so an offline host works once the
cache is warm (bake it into the image, or set ``extension_directory``). The extension is ABI-bound
to the DuckDB version: tested with :data:`DUCKDB_TESTED` (``duckdb>=1.1`` is a range in
``pyproject.toml``; pin it in the lock/image). If the extension cannot be loaded, ``DuckSession``
raises :class:`ColdStoreUnavailable`; callers may fall back to reading via an ``ObjectStore``.
"""

import re
import threading
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import pyarrow as pa

from tidal.common.config import Settings
from tidal.common.errors import DependencyUnavailable
from tidal.storage import layout

DUCKDB_TESTED = "2.0.0"
_ACCOUNT = re.compile(r"[a-z0-9]{3,24}")


class ColdStoreUnavailable(DependencyUnavailable):
    """The cold store (Parquet via DuckDB) cannot be reached or its extension cannot be loaded."""

    slug = "cold-store-unavailable"
    title = "Cold store unavailable"


def glob_for(dataset: str, instrument_id: str) -> str:
    """Container-qualified glob of every part of one instrument/dataset, built from ``layout``."""
    one = layout.md_partition_prefix(dataset, instrument_id, 2000, 0 if dataset == "bars_1d" else 1)
    return one.rsplit("/year=", 1)[0] + "/year=*/month=*/part-*.parquet"


class DuckSession:
    """One DuckDB connection configured for a storage backend; cursors are per-query."""

    def __init__(
        self, conn: duckdb.DuckDBPyConnection, *, local_root: Path | None, container_ping: str
    ) -> None:
        """Wrap a configured ``conn``; use :func:`local_session` / :func:`azure_session`."""
        self._conn = conn
        self._local_root = local_root
        self._ping = container_ping
        self._lock = threading.Lock()

    def uri(self, path: str) -> str:
        """Backend URI of a container-qualified layout path (glob characters pass through)."""
        if self._local_root is not None:
            return (self._local_root / path).as_posix()
        return f"az://{path}"

    def query(self, sql: str, params: list[object]) -> pa.Table:
        """Run ``sql`` with bound ``params`` and return an arrow table.

        Raises:
            FileNotFoundError: no Parquet file matches the pattern (nothing cold exists yet).
            ColdStoreUnavailable: any other DuckDB I/O failure.
        """
        with self._lock:
            cur = self._conn.cursor()
        try:
            table: pa.Table = cur.sql(sql, params=params).to_arrow_table()
            return table
        except duckdb.IOException as exc:
            if "No files found" in str(exc):
                raise FileNotFoundError(str(exc)) from None
            raise ColdStoreUnavailable("cold store read failed") from exc
        finally:
            cur.close()

    def ping(self) -> None:
        """Raise when the cold store is unreachable (local root missing or blob listing fails)."""
        if self._local_root is not None:
            if not self._local_root.is_dir():
                raise ColdStoreUnavailable("local cold root missing")
            return
        try:
            self.query("SELECT 1 FROM glob(?) LIMIT 1", [self.uri(self._ping)])
        except FileNotFoundError:
            return
        except duckdb.Error as exc:
            raise ColdStoreUnavailable("cold store unreachable") from exc


def _harden(conn: duckdb.DuckDBPyConnection) -> None:
    """No implicit extension downloads; freeze settings so queries cannot change them."""
    conn.execute("SET autoinstall_known_extensions = false")
    conn.execute("SET autoload_known_extensions = false")
    conn.execute("SET lock_configuration = true")


def local_session(root: Path) -> DuckSession:
    """Session reading the ``LocalStore`` tree under ``root``."""
    conn = duckdb.connect()
    _harden(conn)
    return DuckSession(conn, local_root=root, container_ping="")


def azure_session(
    *,
    account_url: str,
    managed_identity: bool,
    connection_string: str | None,
    allow_install: bool = False,
) -> DuckSession:
    """Session over Azurite/Azure Blob via the ``azure`` extension (see module docstring).

    ``allow_install`` (local dev/tests only) runs ``INSTALL azure`` first; otherwise the extension
    must already be present in the image's ``extension_directory`` and only ``LOAD`` runs.
    """
    account = (urlparse(account_url).hostname or "").split(".")[0]
    if managed_identity and not _ACCOUNT.fullmatch(account):  # DDL cannot be parameterised
        raise ColdStoreUnavailable("storage account name is not a valid Azure account")
    if not managed_identity and not connection_string:
        raise ColdStoreUnavailable("no storage credentials: need MI or a connection string")
    conn = duckdb.connect()
    try:
        if allow_install:
            conn.execute("INSTALL azure")
        conn.execute("LOAD azure")
        if managed_identity:
            conn.execute(
                "CREATE SECRET tidal_az (TYPE azure, PROVIDER credential_chain, "
                f"ACCOUNT_NAME '{account}')"
            )
        else:
            conn.execute("SET GLOBAL azure_storage_connection_string = ?", [connection_string])
        _harden(conn)
    except duckdb.Error as exc:
        raise ColdStoreUnavailable("cannot load the duckdb azure extension") from exc
    return DuckSession(
        conn, local_root=None, container_ping=f"{layout.CONTAINER_MARKETDATA}/v1/ticks/*"
    )


def session_from_settings(settings: Settings, *, local_root: Path | None = None) -> DuckSession:
    """Factory: ``local_root`` wins (dev/tests), else Azure per ``settings.storage``."""
    if local_root is not None:
        return local_session(local_root)
    s = settings.storage
    cs = s.connection_string.get_secret_value() if s.connection_string else None
    return azure_session(
        account_url=s.account_url,
        managed_identity=s.use_managed_identity,
        connection_string=cs,
        allow_install=settings.env == "local",
    )
