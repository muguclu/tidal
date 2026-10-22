"""Shared machinery for the file-backed candidates F3-F6 (SPEC-02 section 2).

Every file format stores the dataset as one object per instrument-month under the storage-layout
tree (restatements as a second part) and answers W2-W6 with DuckDB SQL over the object list. The
formats differ only in how the bytes are written and how the timestamp is read back
(:attr:`FileFormat.reader`, :attr:`FileFormat.ns_expr`).

Two backends, same code:

* local (default): a :class:`~tidal.storage.parquet_writer.LocalStore` under the work directory;
  bytes read come from the DuckDB profile (Parquet) or the file sizes (CSV, whose profile counts
  decompressed bytes);
* Azurite / Blob: pass a :class:`BlobTarget`. Objects go to a throwaway container; DuckDB's
  ``azure`` extension and the listing client talk to Azurite through a :class:`CountingProxy`, so
  the **request count and the transferred bytes are real HTTP counts** (SPEC-02 section 4,
  EUR per 1,000 queries = transactions + egress). The numbers are exposed as
  :attr:`FileFormat.last_requests` after :meth:`FileFormat.io_profile`.
"""

# ruff: noqa: S608 - SQL is assembled from class constants; every value is a bound parameter

import contextlib
import http.client
import io
import json
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import urlsplit

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from benchmarks.storage.workloads import Dataset, LoadStats, Value, WorkloadResult
from tidal.common.errors import ConfigError
from tidal.storage import layout
from tidal.storage.parquet_writer import AzureBlobStore, LocalStore, ObjectStore

if TYPE_CHECKING:
    from azure.storage.blob import BlobServiceClient

NS_DAY: Final = 86_400 * 10**9
_W2_KEYS: Final = ("n_days", "n_trades", "sum_open", "sum_high", "sum_low", "sum_close")
_HOP_BY_HOP: Final = frozenset({"connection", "keep-alive", "transfer-encoding", "te", "upgrade"})
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_BLOB_ENDPOINT = re.compile(r"BlobEndpoint=[^;]*;?")


@dataclass(frozen=True)
class BlobTarget:
    """An Azurite/Azure account: the write client and the connection string DuckDB will use."""

    service: "BlobServiceClient"
    connection_string: str


@dataclass(frozen=True)
class RequestStats:
    """HTTP requests the storage service received during one workload execution."""

    requests: int
    get: int
    head: int
    list: int
    other: int
    egress_bytes: int  # response body bytes sent back by the service


class _Handler(BaseHTTPRequestHandler):
    """Forwards one request to the upstream service and tallies it."""

    protocol_version = "HTTP/1.1"
    proxy: "CountingProxy"

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log."""

    def _forward(self) -> None:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # Reads (the only traffic we count) never send chunked bodies; refuse instead of
            # silently forwarding a body we did not frame.
            self.send_response(501)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
        up = http.client.HTTPConnection(self.proxy.upstream_host, self.proxy.upstream_port)
        try:
            up.request(self.command, self.path, body=body, headers=headers)
            resp = up.getresponse()
            data = resp.read() if self.command != "HEAD" else b""
            out_headers = [(k, v) for k, v in resp.getheaders() if k.lower() not in _HOP_BY_HOP]
            status = resp.status
        finally:
            up.close()
        self.proxy.tally(self.command, self.path, len(data))
        self.send_response(status)
        has_len = False
        for k, v in out_headers:
            has_len |= k.lower() == "content-length"
            if k.lower() not in {"server", "date"}:
                self.send_header(k, v)
        if not has_len:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _forward  # noqa: N815


class CountingProxy:
    """Transparent HTTP forwarder in front of Azurite that counts requests and response bytes.

    The storage account key signs method, headers and the canonical resource (account + path), not
    the host, so a connection string with ``BlobEndpoint`` pointed here authenticates unchanged.
    """

    def __init__(self, upstream_url: str) -> None:
        """Listen on an ephemeral localhost port, forwarding to ``upstream_url``."""
        u = urlsplit(upstream_url)
        if u.scheme != "http" or (u.hostname or "") not in _LOOPBACK_HOSTS:
            raise ConfigError("CountingProxy forwards plain http to a loopback emulator only")
        self.upstream_host = u.hostname or "127.0.0.1"
        self.upstream_port = u.port or 80
        self._lock = threading.Lock()
        self._counts = {"get": 0, "head": 0, "list": 0, "other": 0}
        self._egress = 0
        handler = type("Handler", (_Handler,), {"proxy": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        """The local port clients should use."""
        return int(self._server.server_address[1])

    def tally(self, method: str, path: str, body_bytes: int) -> None:
        """Record one request."""
        kind = "other"
        if method == "HEAD":
            kind = "head"
        elif method == "GET":
            kind = "list" if "comp=list" in path else "get"
        with self._lock:
            self._counts[kind] += 1
            self._egress += body_bytes

    def snapshot(self) -> RequestStats:
        """Cumulative counters so far; subtract two snapshots for one execution."""
        with self._lock:
            c = self._counts
            return RequestStats(
                sum(c.values()), c["get"], c["head"], c["list"], c["other"], self._egress
            )

    def stop(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()


def diff_stats(after: RequestStats, before: RequestStats) -> RequestStats:
    """Requests/bytes between two :meth:`CountingProxy.snapshot` calls."""
    return RequestStats(
        after.requests - before.requests,
        after.get - before.get,
        after.head - before.head,
        after.list - before.list,
        after.other - before.other,
        after.egress_bytes - before.egress_bytes,
    )


def via_proxy(connection_string: str, port: int) -> str:
    """``connection_string`` with ``BlobEndpoint`` pointed at the local proxy."""
    m = re.search(r"AccountName=([^;]+)", connection_string)
    if m is None:
        raise ConfigError("connection string has no AccountName")
    endpoint = f"BlobEndpoint=http://127.0.0.1:{port}/{m.group(1)};"
    if _BLOB_ENDPOINT.search(connection_string):
        return _BLOB_ENDPOINT.sub(endpoint, connection_string, count=1)
    return connection_string.rstrip(";") + ";" + endpoint


def upstream_of(connection_string: str) -> str:
    """The ``BlobEndpoint`` URL of a connection string (the proxy's upstream)."""
    m = re.search(r"BlobEndpoint=([^;]+)", connection_string)
    if m is None:
        raise ConfigError("connection string has no BlobEndpoint")
    return m.group(1)


class RebasedStore:
    """Maps the layout's ``marketdata/`` container onto a private container of ``inner``.

    Lets several formats share one account without colliding while all code keeps using the
    contract paths from :mod:`tidal.storage.layout`.
    """

    def __init__(self, inner: ObjectStore, container: str) -> None:
        """Wrap ``inner``; every path's first component becomes ``container``."""
        self.inner = inner
        self.container = container

    def _to_inner(self, path: str) -> str:
        _, name = layout.split_container(path)
        return f"{self.container}/{name}"

    def _from_inner(self, path: str) -> str:
        _, name = layout.split_container(path)
        return f"{layout.CONTAINER_MARKETDATA}/{name}"

    def put_new(self, path: str, local_file: Path) -> None:
        """Create-only upload."""
        self.inner.put_new(self._to_inner(path), local_file)

    def read(self, path: str) -> bytes:
        """Object bytes."""
        return self.inner.read(self._to_inner(path))

    def exists(self, path: str) -> bool:
        """Whether the object exists."""
        return self.inner.exists(self._to_inner(path))

    def list(self, prefix: str) -> Iterator[str]:
        """Paths under ``prefix`` in contract form."""
        _, _, name = prefix.partition("/")
        return (self._from_inner(p) for p in self.inner.list(f"{self.container}/{name}"))

    def delete(self, path: str) -> None:
        """Delete one object."""
        self.inner.delete(self._to_inner(path))


def put_file(store: ObjectStore, path: str, write: Callable[[Path], None]) -> None:
    """Write via ``write(tmp_path)`` into a temp dir, then ``put_new`` (immutable)."""
    with tempfile.TemporaryDirectory(prefix="tidal-bench-part-") as d:
        tmp = Path(d) / "part"
        write(tmp)
        store.put_new(path, tmp)


class FileFormat:
    """Base for F3-F6: layout, DuckDB queries, backends and I/O accounting."""

    id: str
    storage_price_key: str = "blob_hot_stored_gb_month"
    #: SQL table expression reading a LIST of object URIs bound as ``?``; exposes the tick columns
    reader: ClassVar[str] = "read_parquet(?)"
    #: SQL timestamp column and an expression giving its exact epoch nanoseconds
    ts_col: ClassVar[str] = "ts_ns"
    ns_expr: ClassVar[str] = "ts_ns"
    ts_bound: ClassVar[str] = "?"  # how an epoch-ns bound is compared with ``ts_col``
    compressed_input: ClassVar[bool] = False  # profile counts decompressed bytes -> use file sizes

    def __init__(self, workdir: Path, target: BlobTarget | None = None) -> None:
        """``workdir`` holds the local store/profile; ``target`` switches to Azurite/Blob."""
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)  # profile output lives here
        self.target = target
        self.slug = re.sub(r"[^a-z0-9]+", "-", self.id.lower()).strip("-")
        self.container = f"bench-{self.slug}-{uuid.uuid4().hex[:8]}"[:63]
        self.store: ObjectStore
        self._read_store: ObjectStore
        if target is None:
            self.store = self._read_store = LocalStore(workdir / self.slug)
        else:
            self.store = RebasedStore(AzureBlobStore(target.service), self.container)
            self._read_store = self.store
        self._proxy: CountingProxy | None = None
        self._con: duckdb.DuckDBPyConnection | None = None
        self._bytes = 0
        self._rows = 0
        self._prof = str(workdir / "profile.json")
        self._profiling = False
        self._created = False
        self.last_requests: RequestStats | None = None
        #: Parquet row groups per written object (path -> count); empty for CSV
        self.row_groups: dict[str, int] = {}

    # --- writing ----------------------------------------------------------------------------
    def _write_partition(
        self, tables: Sequence[pa.Table], inst: str, y: int, m: int, source_hash: str
    ) -> int:
        """Write one instrument-month (or its restatement) as a new part; return the rows."""
        raise NotImplementedError

    def _record_row_groups(self, paths: Sequence[str]) -> None:
        """Remember the row-group count of freshly written Parquet objects."""
        for path in paths:
            if isinstance(self.store, LocalStore):
                meta = pq.ParquetFile(self.store.root / path).metadata
            else:
                meta = pq.ParquetFile(io.BytesIO(self.store.read(path))).metadata
            self.row_groups[path] = meta.num_row_groups

    def row_group_summary(self) -> dict[str, float]:
        """``files``, ``total``, ``mean`` and ``max`` row groups per Parquet file."""
        n = list(self.row_groups.values())
        if not n:
            return {"files": 0, "total": 0, "mean": 0.0, "max": 0}
        return {"files": len(n), "total": sum(n), "mean": sum(n) / len(n), "max": max(n)}

    def load(self, dataset: Dataset) -> LoadStats:
        """Write the base partitions, then the restatement rows as an extra immutable part."""
        if self.target is not None and not self._created:
            self.target.service.create_container(self.container)
            self._created = True
        key, rows = dataset.spec.key(), 0
        for part in dataset.partitions():
            rows += self._write_partition(dataset.read_partition(part), *part, key)
        if dataset.restated[0] in dataset.instruments:
            tables = dataset.read_partition(dataset.restated, restated=True)
            rows += self._write_partition(tables, *dataset.restated, key)
        return LoadStats(rows, self.size_bytes())

    def size_bytes(self) -> int:
        """Sum of all object sizes (file system or container listing)."""
        if self.target is None:
            return sum(
                p.stat().st_size for p in (self.workdir / self.slug).rglob("*") if p.is_file()
            )
        if not self._created:
            return 0
        client = self.target.service.get_container_client(self.container)
        return sum(int(b.size or 0) for b in client.list_blobs())

    # --- connection -------------------------------------------------------------------------
    def _blob_session(
        self, connection_string: str
    ) -> tuple[duckdb.DuckDBPyConnection, ObjectStore]:
        """DuckDB connection and listing store bound to ``connection_string`` (Azurite/Blob)."""
        from azure.storage.blob import BlobServiceClient  # noqa: PLC0415 - Azure-only path

        con = duckdb.connect()
        try:
            con.execute("LOAD azure")
        except duckdb.Error:
            con.execute("INSTALL azure")  # first use on this host only (needs network)
            con.execute("LOAD azure")
        con.execute("SET azure_storage_connection_string = ?", [connection_string])
        # DuckDB would otherwise serve repeat reads from its in-memory remote-file cache and
        # the request/byte counts of warm runs would be near zero; a stateless API pays them.
        con.execute("SET enable_external_file_cache = false")
        store = RebasedStore(
            AzureBlobStore(BlobServiceClient.from_connection_string(connection_string)),
            self.container,
        )
        return con, store

    def reopen(self) -> None:
        """New DuckDB connection (new HTTP connections for Blob).

        Timed runs always use the direct connection; only :meth:`io_profile` goes through the
        counting proxy, so the proxy hop never inflates wall time.
        """
        if self._con is not None:
            self._con.close()
        self._prof = str(self.workdir / "profile.json")
        self._profiling = False
        if self.target is None:
            self._con = duckdb.connect()
        else:
            self._con, self._read_store = self._blob_session(self.target.connection_string)

    def _uri(self, path: str) -> str:
        if isinstance(self.store, LocalStore):
            return (self.store.root / path).as_posix()
        _, name = layout.split_container(path)
        return f"az://{self.container}/{name}"

    def _files(self, p: Mapping[str, Any]) -> list[str]:  # Any: JSON params
        """URIs of ``p['instruments']`` x ``p['months']`` objects (one LIST per instrument)."""
        wanted = {
            layout.md_partition_prefix("ticks", inst, int(y), int(m))
            for inst in p["instruments"]
            for y, m in p["months"]
        }
        out: list[str] = []
        for inst in p["instruments"]:
            root = layout.md_partition_prefix("ticks", inst, 2000, 1).rsplit("/year=", 1)[0] + "/"
            out += [f for f in self._read_store.list(root) if f.rpartition("/")[0] + "/" in wanted]
        return [self._uri(f) for f in sorted(out)]

    def _local_size(self, uris: Sequence[str]) -> int:
        return sum(Path(u).stat().st_size for u in uris)

    def _q(
        self, sql: str, args: list[Any], uris: Sequence[str]
    ) -> list[tuple[Any, ...]]:  # Any: cells
        if self._con is None:
            self.reopen()
        assert self._con is not None
        out = self._con.execute(sql, args).fetchall()
        if self._profiling:
            prof = json.loads(Path(self._prof).read_text(encoding="utf-8"))
            self._rows += int(prof["query"]["total_rows_scanned"])
            if self.target is None:
                self._bytes += (
                    self._local_size(uris)
                    if self.compressed_input
                    else int(prof["io"]["total_bytes_read"])
                )
        return out

    def io_profile(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON
        """Untimed re-run with profiling: rows scanned, bytes read, (Blob) real request counts.

        On Blob the re-run uses its own connection through the :class:`CountingProxy`.
        """
        if self.target is None:
            return self._profiled(workload, params)
        if self._proxy is None:
            self._proxy = CountingProxy(upstream_of(self.target.connection_string))
        cs = via_proxy(self.target.connection_string, self._proxy.port)
        saved = (self._con, self._read_store)
        con, store = self._blob_session(cs)
        self._con, self._read_store = con, store
        before = self._proxy.snapshot()
        try:
            res = self._profiled(workload, params)
        finally:
            self._con, self._read_store = saved
            con.close()
        self.last_requests = diff_stats(self._proxy.snapshot(), before)
        return WorkloadResult(
            res.value,
            res.rows_scanned,
            self.last_requests.egress_bytes,
            "azurite_http_response_bytes",
        )

    def _profiled(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON
        if self._con is None:
            self.reopen()
        assert self._con is not None
        quoted = self._prof.replace("'", "''")  # the path is interpolated into a PRAGMA
        self._con.execute("PRAGMA enable_profiling='json'")
        self._con.execute(f"PRAGMA profiling_output='{quoted}'")
        self._profiling = True
        try:
            res = self.run(workload, params)
        finally:
            self._profiling = False
            self._con.execute("PRAGMA disable_profiling")
        return WorkloadResult(res.value, res.rows_scanned, res.bytes_read, self._bytes_source())

    def _bytes_source(self) -> str:
        return "file_size" if self.compressed_input else "duckdb_profile"

    # --- workloads --------------------------------------------------------------------------
    def _pred(self) -> str:
        return f"{self.ts_col} >= {self.ts_bound} AND {self.ts_col} < {self.ts_bound}"

    def run(self, workload: str, params: Mapping[str, Any]) -> WorkloadResult:  # Any: JSON params
        """Run W2-W6 as DuckDB SQL over the object list (no profiling in the timed call)."""
        self._bytes = self._rows = 0
        p, src, ns = params, self.reader, self.ns_expr
        uris = self._files(p)
        rng = [] if p["lo_ns"] is None else [p["lo_ns"], p["hi_ns"]]
        v: Value
        if workload == "W2":
            r = self._q(
                "SELECT count(*) AS n_days, sum(n) AS n_trades, sum(o), sum(h), sum(l), sum(c),"
                f" sum(v) FROM (SELECT arg_min(price, {ns}) o, max(price) h, min(price) l,"
                f" arg_max(price, {ns}) c, sum(size::DOUBLE) v, count(*) n FROM {src}"
                f" WHERE version = 1 AND side = 0 GROUP BY {ns} // {NS_DAY})",
                [uris],
                uris,
            )[0]
            v = {k: int(x) for k, x in zip(_W2_KEYS[:2], r[:2], strict=True)}
            v.update({k: float(x) for k, x in zip(_W2_KEYS[2:], r[2:6], strict=True)})
            v["sum_volume"] = float(r[6])
        elif workload == "W3":
            r = self._q(
                "SELECT count(*), sum(price), min(price), max(price)"
                f" FROM {src} WHERE version = 1 AND {self._pred()}",
                [uris, *rng],
                uris,
            )[0]
            v = {
                "n": int(r[0]),
                "sum_price": float(r[1]),
                "min_price": float(r[2]),
                "max_price": float(r[3]),
            }
        elif workload == "W4":
            r = self._q(
                f"SELECT {ns}, price FROM {src} WHERE version = 1 AND side = 0"
                f" ORDER BY {ns} DESC, side DESC LIMIT 1",
                [uris],
                uris,
            )[0]
            v = {"ts_ns": int(r[0]), "price": float(r[1])}
        elif workload == "W5":
            v = {}
            for inst in p["instruments"]:
                one = [u for u in uris if f"instrument={inst}/" in u]  # no second listing
                r = self._q(
                    f"SELECT count(*), sum(price) FROM {src} WHERE version = 1", [one], one
                )[0]
                v[f"n:{inst}"], v[f"sum_price:{inst}"] = int(r[0]), float(r[1])
        elif workload == "W6":
            v = {}
            for k, asof in enumerate(p["asof_ns"]):
                r = self._q(
                    "SELECT count(*), sum(price), count_if(version = 2) FROM (SELECT"
                    f" {ns} AS t, side, arg_max(price, version) AS price, max(version) AS version"
                    f" FROM {src} WHERE ingested_at_ns <= ? GROUP BY {ns}, side)",
                    [uris, asof],
                    uris,
                )[0]
                v[f"n_{k}"], v[f"sum_price_{k}"], v[f"n_restated_{k}"] = (
                    int(r[0]),
                    float(r[1]),
                    int(r[2]),
                )
        else:
            raise ConfigError(f"{self.id} has no workload {workload!r}")
        return WorkloadResult(v, self._rows, self._bytes, self._bytes_source())

    def teardown(self) -> None:
        """Close DuckDB and the proxy; delete the local store or the throwaway container."""
        if self._con is not None:
            self._con.close()
            self._con = None
        if self._proxy is not None:
            self._proxy.stop()
            self._proxy = None
        if self.target is not None and self._created:
            from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

            with contextlib.suppress(ResourceNotFoundError):
                self.target.service.delete_container(self.container)
            self._created = False
        shutil.rmtree(self.workdir, ignore_errors=True)
