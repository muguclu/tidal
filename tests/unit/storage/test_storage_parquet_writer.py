"""Writer: contract settings asserted through Parquet metadata, immutability, validation."""

# ruff: noqa: PT018

import hashlib
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tidal.common.errors import NotFound
from tidal.contracts.schemas import BAR_V1, PARQUET_ENCODINGS, TICK_V1
from tidal.storage import layout
from tidal.storage import parquet_writer as pw

INST = "SYN.BRENT.TICK"
MARCH_2024 = 1709251200 * 10**9  # 2024-03-01T00:00:00Z
DAY = 86_400 * 10**9


def ticks(n: int, start: int = MARCH_2024, step: int = 1_000_000) -> pa.Table:
    rng = np.random.default_rng(7)
    return pa.table(
        {
            "ts_ns": start + np.arange(n, dtype=np.int64) * step,
            "instrument_id": pa.array([INST] * n).dictionary_encode().cast(TICK_V1.field(1).type),
            "price": 80 + rng.standard_normal(n).cumsum(),
            "size": rng.random(n).astype(np.float32),
            "side": rng.integers(0, 3, n).astype(np.int8),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, start, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=TICK_V1,
    )


def bars(n: int) -> pa.Table:
    ts = 1577836800 * 10**9 + np.arange(n, dtype=np.int64) * DAY  # from 2020-01-01
    px = np.linspace(50, 60, n)
    return pa.table(
        {
            "ts_ns": ts,
            "instrument_id": pa.array(["EIA.WTI.SPOT"] * n)
            .dictionary_encode()
            .cast(BAR_V1.field(1).type),
            "interval": pa.array(["1d"] * n).dictionary_encode().cast(BAR_V1.field(2).type),
            "open": px,
            "high": px,
            "low": px,
            "close": px,
            "volume": np.full(n, np.nan),
            "trade_count": pa.array([None] * n, pa.int32()),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": ts,
            "version": np.ones(n, np.int16),
        },
        schema=BAR_V1,
    )


def write(store: pw.ObjectStore, batches: list[pa.Table], **kw: int) -> list[str]:
    return pw.write_partition(
        store,
        dataset="ticks",
        instrument_id=INST,
        year=2024,
        month=3,
        batches=batches,
        source_hash="abc123",
        **kw,
    )


def test_metadata_matches_storage_contract(tmp_path: Path) -> None:
    store = pw.LocalStore(tmp_path)
    n = 2_300_000  # 3 row groups at the real 1M size
    half = n // 2
    [path] = write(store, [ticks(half), ticks(n - half, MARCH_2024 + half * 1_000_000)])
    assert (
        path
        == "marketdata/v1/ticks/instrument=SYN.BRENT.TICK/year=2024/month=03/part-00000.parquet"
    )
    md = pq.ParquetFile(tmp_path / path).metadata
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [10**6, 10**6, 300_000]
    kv = {k.decode(): v.decode() for k, v in (md.metadata or {}).items()}
    assert kv["tidal.schema"] == "TICK_V1" and kv["tidal.source_hash"] == "abc123"
    assert kv["tidal.writer_version"].startswith("tidal-")
    rg = md.row_group(0)
    cols = {rg.column(i).path_in_schema: rg.column(i) for i in range(rg.num_columns)}
    for name, enc in PARQUET_ENCODINGS["TICK_V1"].items():
        got = set(cols[name].encodings)
        # dictionary columns surface as RLE_DICTIONARY (or the legacy PLAIN_DICTIONARY) pages
        want = {"RLE_DICTIONARY", "PLAIN_DICTIONARY"} if enc == "RLE_DICTIONARY" else {str(enc)}
        assert got & want, (name, got)
    for fcol in ("price", "size"):  # floats never use dictionary encoding
        assert not {e for e in cols[fcol].encodings if "DICTIONARY" in e}, fcol
    assert all(c.compression == "ZSTD" for c in cols.values())
    assert all(c.is_stats_set for c in cols.values())
    stats = cols["ts_ns"].statistics
    assert stats is not None and stats.min == MARCH_2024
    assert [(s.column_index, s.descending) for s in rg.sorting_columns] == [(0, False)]
    assert pq.read_table(tmp_path / path).num_rows == n


def test_parts_are_immutable_and_sequenced(tmp_path: Path) -> None:
    store = pw.LocalStore(tmp_path)
    [a] = write(store, [ticks(10)])
    [b] = write(store, [ticks(10)])
    assert (layout.parse_md_path(a).seq, layout.parse_md_path(b).seq) == (0, 1)
    with pytest.raises(pw.ImmutableFileExists):
        store.put_new(a, tmp_path / a)
    assert not list(tmp_path.rglob("*.tmp"))


def test_rolls_part_above_max_rows(tmp_path: Path) -> None:
    paths = write(pw.LocalStore(tmp_path), [ticks(25)], row_group_size=10, max_part_rows=20)
    assert [layout.parse_md_path(p).seq for p in paths] == [0, 1]
    assert [pq.ParquetFile(tmp_path / p).metadata.num_rows for p in paths] == [20, 5]


def test_streams_batches_into_whole_row_groups(tmp_path: Path) -> None:
    def gen() -> Iterator[pa.Table]:
        for i in range(5):
            yield ticks(100, MARCH_2024 + i * 100 * 1_000_000)

    [p] = pw.write_partition(
        pw.LocalStore(tmp_path),
        dataset="ticks",
        instrument_id=INST,
        year=2024,
        month=3,
        batches=gen(),
        source_hash="h",
        row_group_size=250,
    )
    md = pq.ParquetFile(tmp_path / p).metadata
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [250, 250]


def test_rejections_leave_nothing_behind(tmp_path: Path) -> None:
    store = pw.LocalStore(tmp_path)
    t = ticks(10)
    with pytest.raises(pw.UnsortedInput):
        write(store, [t.take([1, 0, *range(2, 10)])])
    with pytest.raises(pw.UnsortedInput):  # second batch goes back in time
        write(store, [ticks(10), ticks(10)])
    with pytest.raises(pw.UnsortedInput):  # outside the month
        write(store, [ticks(10, MARCH_2024 + 40 * DAY)])
    with pytest.raises(pw.SchemaMismatch):
        write(store, [t.drop_columns(["size"])])
    wrong = pa.schema([f.with_type(pa.float64()) if f.name == "size" else f for f in t.schema])
    with pytest.raises(pw.SchemaMismatch):
        write(store, [t.cast(wrong)])
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_empty_input_writes_nothing(tmp_path: Path) -> None:
    assert write(pw.LocalStore(tmp_path), [ticks(0)]) == []


def test_daily_bars_partition_by_year(tmp_path: Path) -> None:
    [p] = pw.write_partition(
        pw.LocalStore(tmp_path),
        dataset="bars_1d",
        instrument_id="EIA.WTI.SPOT",
        year=2020,
        month=0,
        batches=[bars(366)],
        source_hash="h",
    )
    assert "year=2020/month=00/" in p
    md = pq.ParquetFile(tmp_path / p).metadata
    assert (md.metadata or {})[b"tidal.schema"] == b"BAR_V1"
    assert "BYTE_STREAM_SPLIT" in md.row_group(0).column(3).encodings  # open


def test_negative_prices_survive(tmp_path: Path) -> None:
    t = ticks(3).set_column(2, TICK_V1.field("price"), pa.array([10.0, -37.63, 5.0]))
    [p] = write(pw.LocalStore(tmp_path), [t])
    assert pq.read_table(tmp_path / p)["price"].to_pylist() == [10.0, -37.63, 5.0]


def test_helpers() -> None:
    lo, hi = pw.partition_bounds_ns(2024, 3)
    assert lo == MARCH_2024 and hi - lo == 31 * DAY
    lo, hi = pw.partition_bounds_ns(2020, 0)
    assert hi - lo == 366 * DAY
    with pytest.raises(layout.InvalidPath):
        pw.schema_name_for("nope")


def test_same_input_gives_same_bytes(tmp_path: Path) -> None:
    digests = []
    for d in ("a", "b"):
        [p] = write(pw.LocalStore(tmp_path / d), [ticks(5000)])
        digests.append(hashlib.sha256((tmp_path / d / p).read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_batch_larger_than_part_limit_is_sliced(tmp_path: Path) -> None:
    paths = write(pw.LocalStore(tmp_path), [ticks(55)], row_group_size=10, max_part_rows=20)
    assert [pq.ParquetFile(tmp_path / p).metadata.num_rows for p in paths] == [20, 20, 15]
    assert [layout.parse_md_path(p).seq for p in paths] == [0, 1, 2]


def test_foreign_instrument_or_interval_is_rejected(tmp_path: Path) -> None:
    store = pw.LocalStore(tmp_path)
    id_type = TICK_V1.field("instrument_id").type

    def with_ids(ids: list[str]) -> pa.Table:
        col = pa.array(ids).dictionary_encode().cast(id_type)
        return ticks(len(ids)).set_column(1, TICK_V1.field("instrument_id"), col)

    with pytest.raises(pw.PartitionMismatch):
        write(store, [with_ids(["SYN.WTI.TICK"] * 5)])
    with pytest.raises(pw.PartitionMismatch):  # one foreign row among valid ones
        write(store, [with_ids([INST, "SYN.WTI.TICK", INST])])
    with pytest.raises(pw.PartitionMismatch):  # 1d bars written into a 1h partition
        pw.write_partition(
            store,
            dataset="bars_1h",
            instrument_id="EIA.WTI.SPOT",
            year=2020,
            month=1,
            batches=[bars(3)],
            source_hash="h",
        )
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_writer_is_called_with_contract_page_and_codec_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data page size is not exposed in Parquet footer metadata without a page index (which the
    contract does not enable), so assert the settings handed to pyarrow instead."""
    seen: dict[str, object] = {}
    real = pq.ParquetWriter

    def spy(*args: object, **kw: object) -> pq.ParquetWriter:
        seen.update(kw)
        return real(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(pq, "ParquetWriter", spy)
    write(pw.LocalStore(tmp_path), [ticks(5)])
    assert seen["data_page_size"] == 1 << 20
    assert seen["compression"] == "zstd" and seen["compression_level"] == 6
    assert seen["write_statistics"] is True


def test_local_store_read_list_delete(tmp_path: Path) -> None:
    store = pw.LocalStore(tmp_path / "root")
    src = tmp_path / "f.bin"
    src.write_bytes(b"PAR1xyz")
    paths = ["c/v1/b/part-00001.parquet", "c/v1/a/part-00000.parquet", "c/v1x/part-00000.parquet"]
    for p in paths:
        store.put_new(p, src)
    assert store.read(paths[0]) == b"PAR1xyz"
    assert store.exists(paths[0]) and not store.exists("c/v1/nope")
    with pytest.raises(NotFound):
        store.read("c/v1/nope")
    assert list(store.list("c/v1/")) == sorted(paths[:2])
    assert list(store.list("c/v1")) == sorted(paths)
    assert list(store.list("zzz/")) == []
    store.delete(paths[0])
    store.delete(paths[0])  # idempotent
    assert not store.exists(paths[0]) and store.exists(paths[1])
    with pytest.raises(layout.InvalidPath):
        store.delete("../f.bin")
    with pytest.raises(layout.InvalidPath):
        store.read("c/../../x")


def test_local_store_relative_root_and_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = pw.LocalStore(Path("./.data"))
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    store.put_new("c/a/p.bin", src)
    assert list(store.list("c")) == list(store.list("c/")) == ["c/a/p.bin"]
    assert list(store.list("nocontainer")) == []
    with pytest.raises(layout.InvalidPath):
        list(store.list(""))
    assert store.read("c/a/p.bin") == b"x" and store.exists("c/a/p.bin")
    with pytest.raises(layout.InvalidPath):
        store.delete("c/a")  # a directory
    with pytest.raises(layout.InvalidPath):
        store.put_new("../escape.bin", src)
    store.delete("c/a/p.bin")
    assert not store.exists("c/a/p.bin")
