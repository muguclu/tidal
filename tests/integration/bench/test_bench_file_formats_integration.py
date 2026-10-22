"""F3-F6 against the compose Azurite (T-203): oracle-identical results and REAL request counts.

Needs ``TIDAL_STORAGE__CONNECTION_STRING`` (Azurite dev account, as in ``.env``) and the duckdb
``azure`` extension (installed on first use). Every container a test creates is removed by a
fixture finaliser, also when the test fails.
"""

import contextlib
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from benchmarks.storage.formats import file_formats
from benchmarks.storage.formats.file_base import (
    BlobTarget,
    CountingProxy,
    FileFormat,
    upstream_of,
    via_proxy,
)
from benchmarks.storage.workloads import (
    WORKLOADS,
    Dataset,
    DatasetSpec,
    build_dataset,
    values_match,
)

pytestmark = pytest.mark.integration

SPEC = DatasetSpec(
    scale="S", seed=7, start=date(2024, 12, 1), end=date(2024, 12, 31), n_instruments=2
)
Make = Callable[[str, Path], FileFormat]


@pytest.fixture(scope="module")
def target() -> BlobTarget:
    cs = os.environ.get("TIDAL_STORAGE__CONNECTION_STRING")
    if not cs:
        pytest.skip("TIDAL_STORAGE__CONNECTION_STRING (Azurite) is required")
    return BlobTarget(BlobServiceClient.from_connection_string(cs), cs)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    return build_dataset(SPEC, tmp_path_factory.mktemp("bench-file-data"))


@pytest.fixture
def make(target: BlobTarget) -> Iterator[Make]:
    """Factory for Blob-backed formats; every one is torn down when the test ends."""
    made: list[FileFormat] = []

    def build(fid: str, workdir: Path) -> FileFormat:
        fmt = file_formats(target)[fid](workdir)
        assert isinstance(fmt, FileFormat)
        made.append(fmt)  # registered before use, so a failing load is still cleaned up
        return fmt

    yield build
    for fmt in made:
        fmt.teardown()


@pytest.fixture(scope="module")
def local_sizes(dataset: Dataset, tmp_path_factory: pytest.TempPathFactory) -> dict[str, int]:
    """At-rest bytes of each format on the local file system (the blob run must match)."""
    out: dict[str, int] = {}
    root = tmp_path_factory.mktemp("bench-file-local")
    for fid in ("F3", "F4", "F5", "F6-z3-rg128k"):
        fmt = file_formats()[fid](root / fid)
        try:
            out[fid] = fmt.load(dataset).bytes_written
        finally:
            fmt.teardown()
    return out


@pytest.mark.parametrize("fid", ["F3", "F4", "F5", "F6-z3-rg128k", "F6-z9-rg4m"])
def test_blob_backed_format_matches_oracle_and_counts_requests(
    fid: str,
    *,
    target: BlobTarget,
    dataset: Dataset,
    tmp_path: Path,
    make: Make,
    local_sizes: dict[str, int],
) -> None:
    fmt = make(fid, tmp_path)
    stats = fmt.load(dataset)
    assert stats.rows > sum(dataset.rows.values())
    if fid in local_sizes:
        assert stats.bytes_written == local_sizes[fid]  # same bytes in Blob as on disk
    for wid in ("W2", "W3", "W4", "W5", "W6"):
        p = WORKLOADS[wid].params(dataset)
        want = WORKLOADS[wid].oracle(dataset, p)
        assert values_match(fmt.run(wid, p).value, want), (fid, wid)
        io = fmt.io_profile(wid, p)
        assert values_match(io.value, want), (fid, wid, "profiled")
        req = fmt.last_requests
        assert req is not None
        assert req.requests == req.get + req.head + req.list + req.other
        assert req.list >= 1  # the object listing is counted too
        assert req.get >= 1
        assert io.bytes_read == req.egress_bytes > 0
        assert io.bytes_read_source == "azurite_http_response_bytes"
    # the timed path never touched the proxy: a run() between two snapshots adds nothing
    assert fmt._proxy is not None
    before = fmt._proxy.snapshot()
    fmt.reopen()  # a cold connection keeps working
    p = WORKLOADS["W2"].params(dataset)
    assert values_match(fmt.run("W2", p).value, WORKLOADS["W2"].oracle(dataset, p))
    assert fmt._proxy.snapshot() == before
    container = fmt.container
    fmt.teardown()
    with pytest.raises(ResourceNotFoundError):
        target.service.get_container_client(container).get_container_properties()


def test_timed_runs_do_not_start_the_proxy(dataset: Dataset, tmp_path: Path, make: Make) -> None:
    fmt = make("F5", tmp_path)
    fmt.load(dataset)
    p = WORKLOADS["W3"].params(dataset)
    assert values_match(fmt.run("W3", p).value, WORKLOADS["W3"].oracle(dataset, p))
    assert fmt._proxy is None


def test_row_groups_are_recorded_on_blob(dataset: Dataset, tmp_path: Path, make: Make) -> None:
    fmt = make("F6-z3-rg128k", tmp_path)
    fmt.load(dataset)
    s = fmt.row_group_summary()
    assert s["files"] >= 3
    assert s["max"] > 1


def test_parquet_range_reads_use_fewer_bytes_than_full_csv(
    dataset: Dataset, tmp_path: Path, make: Make
) -> None:
    """W3 (one month): Parquet fetches footer + needed column chunks, CSV the whole object."""
    egress: dict[str, int] = {}
    for fid in ("F3", "F5"):
        fmt = make(fid, tmp_path / fid)
        fmt.load(dataset)
        fmt.io_profile("W3", WORKLOADS["W3"].params(dataset))
        assert fmt.last_requests is not None
        egress[fid] = fmt.last_requests.egress_bytes
        fmt.teardown()
    assert egress["F5"] < egress["F3"]


@pytest.fixture
def scratch_container(target: BlobTarget) -> Iterator[str]:
    """A uniquely named container, deleted by the finaliser even when the test fails."""
    name = f"bench-proxy-test-{uuid.uuid4().hex[:10]}"
    yield name
    with contextlib.suppress(ResourceNotFoundError):
        target.service.delete_container(name)


def test_counting_proxy_tallies_method_kinds(target: BlobTarget, scratch_container: str) -> None:
    proxy = CountingProxy(upstream_of(target.connection_string))
    try:
        svc = BlobServiceClient.from_connection_string(
            via_proxy(target.connection_string, proxy.port)
        )
        c = svc.create_container(scratch_container)
        c.upload_blob("a.bin", b"x" * 1000)
        assert c.get_blob_client("a.bin").download_blob().readall() == b"x" * 1000
        assert c.get_blob_client("a.bin").get_blob_properties().size == 1000
        assert [b.name for b in c.list_blobs()] == ["a.bin"]
        s = proxy.snapshot()
        assert s.get >= 1
        assert s.head >= 1
        assert s.list >= 1
        assert s.other >= 2  # create, upload
        assert s.egress_bytes >= 1000
    finally:
        proxy.stop()


def test_no_bench_containers_leak(target: BlobTarget) -> None:
    """Runs last in the module: nothing named bench-* may survive the other tests' finalisers."""
    # F5/F3 containers of finished tests are already gone; the module-scoped fixtures own none.
    left = [c.name for c in target.service.list_containers(name_starts_with="bench-")]
    assert [n for n in left if n.startswith(("bench-f", "bench-proxy-test-"))] == []
