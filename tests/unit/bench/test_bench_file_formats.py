"""File formats F3-F6: correctness vs the oracle, determinism, F6 sweep, counting proxy (T-203)."""

import hashlib
import http.client
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pytest
from benchmarks.storage.formats import ALL_FORMATS, f6_ids, file_formats
from benchmarks.storage.formats.file_base import (
    CountingProxy,
    FileFormat,
    RebasedStore,
    diff_stats,
    upstream_of,
    via_proxy,
)
from benchmarks.storage.formats.file_f3 import _COLUMNS, to_csv_bytes
from benchmarks.storage.formats.file_f4 import to_naive
from benchmarks.storage.run import FORMATS, F5Format
from benchmarks.storage.workloads import WORKLOADS, Dataset, values_match
from hypothesis import given, settings
from hypothesis import strategies as st

from tidal.common.errors import ConfigError
from tidal.contracts.schemas import TICK_V1
from tidal.storage import layout
from tidal.storage import parquet_writer as pw
from tidal.storage.parquet_writer import LocalStore

FILE_IDS = ["F3", "F4", "F5", "F6", "F6-z3-rg128k", "F6-z9-rg4m"]


def test_registry_covers_f0_to_f6_and_the_full_sweep() -> None:
    assert {"F0", "F1", "F2", "F3", "F4", "F5", "F6"} <= set(FORMATS)
    ids = f6_ids()
    assert len(ids) == 9
    assert set(ids) <= set(FORMATS)
    assert "F6-z6-rg1m" in ids
    assert ids[0] == "F6-z3-rg128k"
    assert set(file_formats()) == set(ALL_FORMATS) - {"F0", "F1", "F2"}


@pytest.mark.parametrize("fid", FILE_IDS)
def test_format_matches_oracle_on_all_workloads(
    fid: str, tiny_dataset: Dataset, tmp_path: Path
) -> None:
    fmt = FORMATS[fid](tmp_path / "w")
    stats = fmt.load(tiny_dataset)
    assert stats.rows > sum(tiny_dataset.rows.values())  # base + restatements
    assert stats.bytes_written == fmt.size_bytes() > 0
    for wid in ("W2", "W3", "W4", "W5", "W6"):
        p = WORKLOADS[wid].params(tiny_dataset)
        want = WORKLOADS[wid].oracle(tiny_dataset, p)
        assert values_match(fmt.run(wid, p).value, want), (fid, wid)
        io = fmt.io_profile(wid, p)
        assert values_match(io.value, want), (fid, wid)
        assert io.bytes_read is not None
        assert io.bytes_read > 0
        assert io.rows_scanned
    fmt.reopen()
    p = WORKLOADS["W2"].params(tiny_dataset)
    assert values_match(fmt.run("W2", p).value, WORKLOADS["W2"].oracle(tiny_dataset, p))
    fmt.teardown()
    assert not (tmp_path / "w").exists()


def test_w2_identical_across_file_formats_and_f5_is_smallest(
    tiny_dataset: Dataset, tmp_path: Path
) -> None:
    p = WORKLOADS["W2"].params(tiny_dataset)
    sizes: dict[str, int] = {}
    ref = None
    for fid in ("F3", "F4", "F5"):
        fmt = FORMATS[fid](tmp_path / fid)
        sizes[fid] = fmt.load(tiny_dataset).bytes_written
        v = fmt.run("W2", p).value
        ref = ref or v
        assert values_match(v, ref), fid
        fmt.teardown()
    assert sizes["F5"] < sizes["F4"]
    assert sizes["F5"] < sizes["F3"]


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix != ".json"):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()


@pytest.mark.parametrize("fid", ["F3", "F4", "F5", "F6-z9-rg128k"])
def test_written_bytes_are_deterministic(fid: str, tiny_dataset: Dataset, tmp_path: Path) -> None:
    hashes = []
    for i in range(2):
        fmt = FORMATS[fid](tmp_path / str(i))
        assert isinstance(fmt, FileFormat)
        fmt.load(tiny_dataset)
        hashes.append(_tree_hash(tmp_path / str(i)))
        fmt.teardown()
    assert hashes[0] == hashes[1]


def test_f6_sweep_changes_bytes_and_restores_writer_level(
    tiny_dataset: Dataset, tmp_path: Path
) -> None:
    before = pw.COMPRESSION_LEVEL
    out = {}
    for fid in ("F5", "F6-z3-rg1m", "F6-z9-rg1m", "F6-z3-rg128k"):
        fmt = FORMATS[fid](tmp_path / fid)
        assert isinstance(fmt, FileFormat)
        fmt.load(tiny_dataset)
        out[fid] = _tree_hash(tmp_path / fid)
        fmt.teardown()
    assert pw.COMPRESSION_LEVEL == before == 6
    assert len(set(out.values())) == 4  # level and row-group size both change the bytes


def test_f6_writer_level_restored_after_a_failed_write(
    tiny_dataset: Dataset, tmp_path: Path
) -> None:
    fmt = FORMATS["F6-z9-rg1m"](tmp_path / "x")
    assert isinstance(fmt, F5Format)
    bad = [pa.table({"nope": [1]})]
    with pytest.raises(pw.SchemaMismatch):
        fmt._write_partition(bad, tiny_dataset.instruments[0], 2024, 12, "h")
    assert pw.COMPRESSION_LEVEL == 6


def test_f5_ids_and_defaults(tmp_path: Path) -> None:
    f5 = F5Format(tmp_path / "a")
    assert (f5.id, f5.level, f5.row_group) == ("F5", pw.COMPRESSION_LEVEL, pw.ROW_GROUP_ROWS)
    assert FORMATS["F6"](tmp_path / "b").id == "F6-z9-rg1m"
    assert FORMATS["F6-z6-rg1m"](tmp_path / "c").id == "F6-z6-rg1m"


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(0, 4_102_444_800 * 10**9),
            st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_csv_roundtrip_is_exact_including_negative_prices(rows: list[tuple[int, float]]) -> None:
    n = len(rows)
    t = pa.table(
        {
            "ts_ns": pa.array([r[0] for r in rows], pa.int64()),
            "instrument_id": pa.array(["SYN.WTI.TICK"] * n).dictionary_encode(),
            "price": pa.array([r[1] for r in rows], pa.float64()),
            "size": pa.array([i * 1.37 for i in range(n)], pa.float32()),
            "side": pa.array([0] * n, pa.int8()),
            "quality": pa.array([0] * n, pa.int8()),
            "ingested_at_ns": pa.array([1] * n, pa.int64()),
            "version": pa.array([1] * n, pa.int16()),
        }
    ).cast(TICK_V1)
    raw = to_csv_bytes(t)
    assert raw.startswith(b"ts,instrument_id,price,size,side,quality,ingested_at_ns,version\n")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "x.csv"
        tmp.write_bytes(raw)
        sql = "SELECT epoch_ns(ts), price, size FROM read_csv(?, header = true, columns = "
        got = duckdb.connect().execute(sql + _COLUMNS + ")", [tmp.as_posix()]).fetchall()
    assert [g[0] for g in got] == [r[0] for r in rows]
    assert [g[1] for g in got] == [r[1] for r in rows]  # bit-exact doubles, negatives kept
    assert np.array_equal(
        np.array([g[2] for g in got], dtype=np.float32), t.column("size").to_numpy()
    )


def test_f4_naive_layout_keeps_ns_precision() -> None:
    t = pa.table(
        {
            "ts_ns": pa.array([1_730_422_800_203_615_214], pa.int64()),
            "instrument_id": pa.array(["SYN.WTI.TICK"]).dictionary_encode(),
            "price": [-37.63],
            "size": pa.array([1.5], pa.float32()),
            "side": pa.array([0], pa.int8()),
            "quality": pa.array([0], pa.int8()),
            "ingested_at_ns": pa.array([5], pa.int64()),
            "version": pa.array([1], pa.int16()),
        }
    )
    n = to_naive(t)
    assert n.schema.field("ts").type == pa.timestamp("ns")
    assert n.column("ts").cast(pa.int64()).to_pylist() == [1_730_422_800_203_615_214]
    assert n.schema.field("instrument_id").type == pa.string()


def test_connection_string_helpers() -> None:
    cs = (
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=abc;"
        "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;QueueEndpoint=http://q;"
    )
    assert upstream_of(cs) == "http://127.0.0.1:10000/devstoreaccount1"
    out = via_proxy(cs, 5555)
    assert "BlobEndpoint=http://127.0.0.1:5555/devstoreaccount1;" in out
    assert "QueueEndpoint=http://q;" in out
    assert "AccountKey=abc" in out
    assert "BlobEndpoint=http://127.0.0.1:5555/acc;" in via_proxy("AccountName=acc;", 5555)
    with pytest.raises(ConfigError):
        via_proxy("AccountKey=abc", 1)
    with pytest.raises(ConfigError):
        upstream_of("AccountName=a")


def test_rebased_store_maps_the_container_and_lists_in_contract_form(tmp_path: Path) -> None:
    inner = LocalStore(tmp_path)
    store = RebasedStore(inner, "bench-x")
    src = tmp_path / "f.bin"
    src.write_bytes(b"abc")
    path = layout.md_part_path("ticks", "SYN.WTI.TICK", 2024, 12, 0)
    store.put_new(path, src)
    assert (tmp_path / "bench-x" / path.split("/", 1)[1]).read_bytes() == b"abc"
    assert store.exists(path)
    assert store.read(path) == b"abc"
    prefix = layout.md_partition_prefix("ticks", "SYN.WTI.TICK", 2024, 12)
    assert list(store.list(prefix)) == [path]
    store.delete(path)
    assert not store.exists(path)


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        body = b"<list/>" if "comp=list" in self.path else b"y" * 500
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "500")
        self.end_headers()

    def do_PUT(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def upstream() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/acct"
    srv.shutdown()
    srv.server_close()


def test_counting_proxy_counts_kinds_and_bytes_and_forwards_faithfully(upstream: str) -> None:
    proxy = CountingProxy(upstream)
    try:
        before = proxy.snapshot()
        c = http.client.HTTPConnection("127.0.0.1", proxy.port)
        c.request("GET", "/acct/c/blob")
        r = c.getresponse()
        assert (r.status, r.read()) == (200, b"y" * 500)
        c.request("GET", "/acct/c?restype=container&comp=list")
        assert c.getresponse().read() == b"<list/>"
        c.request("HEAD", "/acct/c/blob")
        r = c.getresponse()
        assert (r.status, r.getheader("Content-Length"), r.read()) == (200, "500", b"")
        c.request("PUT", "/acct/c/blob", body=b"zzz")
        assert c.getresponse().status == 201
        c.close()
        s = diff_stats(proxy.snapshot(), before)
    finally:
        proxy.stop()
    assert (s.requests, s.get, s.list, s.head, s.other) == (4, 1, 1, 1, 1)
    assert s.egress_bytes == 507  # HEAD has no body; PUT response is empty
