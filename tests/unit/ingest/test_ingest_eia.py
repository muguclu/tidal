import gzip
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pytest
import structlog
from pydantic import SecretStr

from tidal.common.errors import ConfigError
from tidal.common.timeutil import FixedClock
from tidal.contracts.instruments import CATALOGUE_V1
from tidal.ingest.sources import eia
from tidal.ingest.sources.base import RawChunk
from tidal.ingest.sources.eia import (
    EiaRequestRejected,
    EiaSource,
    EiaUnavailable,
    parse_eia_page,
    redact_url,
)
from tidal.storage.parquet_writer import LocalStore

FIX = Path(__file__).parents[2] / "fixtures" / "eia"
PAGE1, PAGE2 = (FIX / "rwtc_page1.json").read_bytes(), (FIX / "rwtc_page2.json").read_bytes()
KEY = "SUPERSECRETKEY123"
START, END = date(2020, 4, 16), date(2020, 4, 23)
NS_PER_DAY = 86_400 * 10**9
Handler = Callable[[httpx.Request], httpx.Response]


class Server:
    """MockTransport handler: serves the fixture pages by offset after scripted failures."""

    def __init__(self, failures: list[httpx.Response] | None = None) -> None:
        self.failures = list(failures or [])
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            return self.failures.pop(0)
        offset = int(request.url.params["offset"])
        return httpx.Response(200, content=PAGE1 if offset == 0 else PAGE2)


def make(
    tmp_path: Path, server: Handler, max_pages: int = eia.MAX_PAGES
) -> tuple[EiaSource, list[float], LocalStore]:
    sleeps: list[float] = []
    store = LocalStore(tmp_path)
    src = EiaSource(
        httpx.Client(transport=httpx.MockTransport(server)),
        SecretStr(KEY),
        store,
        np.random.Generator(np.random.PCG64(1)),
        clock=FixedClock(123),
        sleep=sleeps.append,
        max_pages=max_pages,
    )
    return src, sleeps, store


def run(src: EiaSource, instrument: str = "EIA.WTI.SPOT") -> list[RawChunk]:
    return list(src.fetch(instrument, START, END))


def test_pagination_two_pages_saved(tmp_path: Path) -> None:
    server = Server()
    src, _, store = make(tmp_path, server)
    chunks = run(src)
    assert [c.page for c in chunks] == [0, 1]
    assert [c.payload for c in chunks] == [PAGE1, PAGE2]
    assert all(c.source == "EIA" and c.series == "RWTC" and c.fetched_at_ns == 123 for c in chunks)
    assert [int(r.url.params["offset"]) for r in server.requests] == [0, 3]
    q = server.requests[0].url
    assert q.path == "/v2/petroleum/pri/spt/data/"
    assert q.params["length"] == "5000"
    assert q.params["frequency"] == "daily"
    assert q.params["facets[series][]"] == "RWTC"
    assert q.params["sort[0][direction]"] == "asc"
    assert (q.params["start"], q.params["end"]) == ("2020-04-16", "2020-04-23")
    assert store.list_paths("raw/eia/") == [
        "raw/eia/rwtc/20200416.json.gz",
        "raw/eia/rwtc/20200421.json.gz",
    ]
    assert gzip.decompress((tmp_path / "raw/eia/rwtc/20200416.json.gz").read_bytes()) == PAGE1


@pytest.mark.parametrize(
    ("instrument", "route"),
    [
        ("EIA.BRENT.SPOT", "/v2/petroleum/pri/spt/data/"),
        ("EIA.WTI.FUT3", "/v2/petroleum/pri/fut/data/"),
        ("EIA.HH.SPOT", "/v2/natural-gas/pri/fut/data/"),
        ("EIA.HH.FUT4", "/v2/natural-gas/pri/fut/data/"),
    ],
)
def test_routes_per_series(tmp_path: Path, instrument: str, route: str) -> None:
    server = Server()
    src, _, _ = make(tmp_path, server)
    next(src.fetch(instrument, START, END))
    assert server.requests[0].url.path == route


def test_routes_cover_every_eia_series() -> None:
    series = {e.source_series for e in CATALOGUE_V1 if e.instrument_id.startswith("EIA.")}
    assert series == set(eia.ROUTES)


def test_saved_before_first_chunk_is_yielded(tmp_path: Path) -> None:
    src, _, _ = make(tmp_path, Server())
    first = next(src.fetch("EIA.WTI.SPOT", START, END))
    assert (tmp_path / "raw/eia/rwtc/20200416.json.gz").exists()
    assert first.payload == PAGE1


def test_raw_saved_even_if_parse_fails(tmp_path: Path) -> None:
    bad = b'{"response":{"total":"1","data":[{"period":"2020-04-16","series":"RWTC","value":"x"}]}}'
    src, _, store = make(tmp_path, lambda _: httpx.Response(200, content=bad))
    (chunk,) = run(src)
    assert store.list_paths("raw/eia/") == ["raw/eia/rwtc/20200416.json.gz"]
    assert parse_eia_page(chunk.payload).column("value").to_pylist()[0] != 0.0  # NaN, no crash


def test_refetch_is_idempotent(tmp_path: Path) -> None:
    src, _, store = make(tmp_path, Server())
    run(src)
    before = {p: (tmp_path / p).read_bytes() for p in store.list_paths("raw/")}
    run(src)
    assert {p: (tmp_path / p).read_bytes() for p in store.list_paths("raw/")} == before


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_then_success(tmp_path: Path, status: int) -> None:
    src, sleeps, _ = make(tmp_path, Server([httpx.Response(status), httpx.Response(status)]))
    assert len(run(src)) == 2
    assert len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 1.0
    assert 1.0 <= sleeps[1] <= 2.0


def test_retry_after_is_lower_bound(tmp_path: Path) -> None:
    server = Server([httpx.Response(429, headers={"Retry-After": "7"})])
    src, sleeps, _ = make(tmp_path, server)
    run(src)
    assert sleeps == [7.0]


def test_jitter_is_seeded(tmp_path: Path) -> None:
    runs = []
    for sub in ("a", "b"):
        src, sleeps, _ = make(tmp_path / sub, Server([httpx.Response(500)] * 3))
        run(src)
        runs.append(sleeps)
    assert runs[0] == runs[1]
    assert len(set(runs[0])) == 3


def test_transport_error_retried(tmp_path: Path) -> None:
    calls = {"n": 0}
    inner = Server()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return inner(request)

    src, sleeps, _ = make(tmp_path, handler)
    assert len(run(src)) == 2
    assert len(sleeps) == 1


def test_gives_up_after_max_attempts(tmp_path: Path) -> None:
    server = Server([httpx.Response(503)] * 10)
    src, sleeps, _ = make(tmp_path, server)
    with pytest.raises(EiaUnavailable) as ei:
        run(src)
    assert len(server.requests) == eia.MAX_ATTEMPTS
    assert len(sleeps) == eia.MAX_ATTEMPTS - 1
    assert KEY not in str(ei.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_4xx_fails_fast_without_leaking_key(tmp_path: Path, status: int) -> None:
    server = Server([httpx.Response(status)])
    src, sleeps, _ = make(tmp_path, server)
    with structlog.testing.capture_logs() as logs, pytest.raises(EiaRequestRejected) as ei:
        run(src)
    assert len(server.requests) == 1
    assert sleeps == []
    assert str(status) in str(ei.value)
    assert KEY not in str(ei.value)
    assert KEY not in repr(logs)
    assert "api_key=**********" in repr(logs)


def test_key_in_query_but_never_in_logs_or_paths(tmp_path: Path) -> None:
    server = Server([httpx.Response(500)])
    src, _, store = make(tmp_path, server)
    with structlog.testing.capture_logs() as logs:
        run(src)
    assert server.requests[0].url.params["api_key"] == KEY
    assert logs
    assert KEY not in repr(logs)
    assert all(KEY not in p for p in store.list_paths("raw/"))


def test_redact_url() -> None:
    assert redact_url("https://x/y?a=1&api_key=abc&b=2") == "https://x/y?a=1&api_key=**********&b=2"


def test_missing_key_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        EiaSource(httpx.Client(), None, LocalStore(tmp_path), np.random.default_rng(0))


def test_max_pages_cap(tmp_path: Path) -> None:
    endless = PAGE1.replace(b'"total": "6"', b'"total": "999999"')
    src, _, _ = make(tmp_path, lambda _: httpx.Response(200, content=endless), max_pages=3)
    it = src.fetch("EIA.WTI.SPOT", START, END)
    assert [next(it).page for _ in range(3)] == [0, 1, 2]
    with pytest.raises(EiaUnavailable):
        next(it)


def test_malformed_page_rejected(tmp_path: Path) -> None:
    src, _, _ = make(tmp_path, lambda _: httpx.Response(200, content=b"<html>"))
    with pytest.raises(EiaUnavailable):
        run(src)
    with pytest.raises(EiaUnavailable):
        parse_eia_page(b"nope")


def test_empty_page_stops(tmp_path: Path) -> None:
    empty = b'{"response":{"total":"0","data":[]}}'
    src, _, _ = make(tmp_path, lambda _: httpx.Response(200, content=empty))
    assert len(run(src)) == 1
    assert parse_eia_page(empty).num_rows == 0


def test_golden_wti_negative_and_adr0009_timestamps() -> None:
    t = parse_eia_page(PAGE1)
    assert t.column_names == ["ts_ns", "value", "series"]
    assert str(t.schema.field("ts_ns").type) == "int64"
    assert str(t.schema.field("value").type) == "double"
    ts = t.column("ts_ns").to_numpy()
    rows = dict(zip(ts.tolist(), t.column("value").to_numpy().tolist(), strict=True))
    assert rows[1_587_340_800 * 10**9] == -37.63  # 2020-04-20T00:00:00Z
    assert (ts % NS_PER_DAY == 0).all()
    assert list(rows) == sorted(rows)
    assert set(t.column("series").to_pylist()) == {"RWTC"}


def test_null_value_becomes_nan() -> None:
    body = (
        b'{"response":{"total":"2","data":[{"period":"2020-01-02","series":"RWTC","value":null},'
        b'{"period":"2020-01-03","series":"RWTC","value":"1.5"}]}}'
    )
    v = parse_eia_page(body).column("value").to_pylist()
    assert v[0] != v[0]
    assert v[1] == 1.5
