import gzip
import hashlib
import json
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
from tidal.ingest.sources import base, eia, fred
from tidal.ingest.sources.base import (
    RawChunk,
    raw_blob_path,
    redact_url,
    strip_api_key,
    strip_credential_echo,
)
from tidal.ingest.sources.eia import (
    EIA_FUTURES_LAST_DATE,
    EiaRequestRejected,
    EiaSource,
    EiaUnavailable,
    parse_eia_page,
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
    paths = [raw_blob_path(c) for c in chunks]
    assert paths[0].startswith("raw/eia/rwtc/20200416-")
    assert paths[1].startswith("raw/eia/rwtc/20200421-")
    assert store.list_paths("raw/eia/") == sorted(paths)
    assert gzip.decompress((tmp_path / paths[0]).read_bytes()) == PAGE1


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
    assert (tmp_path / raw_blob_path(first)).exists()
    assert first.payload == PAGE1


def test_raw_saved_even_if_parse_fails(tmp_path: Path) -> None:
    bad = b'{"response":{"total":"1","data":[{"period":"2020-04-16","series":"RWTC","value":"x"}]}}'
    src, _, store = make(tmp_path, lambda _: httpx.Response(200, content=bad))
    (chunk,) = run(src)
    assert store.list_paths("raw/eia/") == [raw_blob_path(chunk)]
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
    assert len(server.requests) == base.MAX_ATTEMPTS
    assert len(sleeps) == base.MAX_ATTEMPTS - 1
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
    assert rows[1_587_340_800 * 10**9] == -36.98  # 2020-04-20T00:00:00Z, RWTC spot
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


def test_golden_wti_fut1_negative() -> None:
    t = parse_eia_page((FIX / "rclc1_page1.json").read_bytes())
    rows = dict(zip(t.column("ts_ns").to_pylist(), t.column("value").to_pylist(), strict=True))
    assert rows[1_587_340_800 * 10**9] == -37.63  # NYMEX settle, RCLC1
    assert set(t.column("series").to_pylist()) == {"RCLC1"}


def test_futures_end_clipped_to_discontinuation(tmp_path: Path) -> None:
    server = Server()
    src, _, _ = make(tmp_path, server)
    list(src.fetch("EIA.WTI.FUT1", date(2024, 1, 1), date(2025, 1, 1)))
    assert server.requests[0].url.params["end"] == EIA_FUTURES_LAST_DATE.isoformat() == "2024-04-05"
    assert server.requests[0].url.path == "/v2/petroleum/pri/fut/data/"
    assert list(src.fetch("EIA.HH.FUT2", date(2024, 5, 1), date(2025, 1, 1))) == []
    assert len(server.requests) == 2  # the second range made no request at all
    list(src.fetch("EIA.WTI.SPOT", date(2024, 1, 1), date(2025, 1, 1)))
    assert server.requests[-1].url.params["end"] == "2025-01-01"  # spot is not clipped


def test_different_payloads_same_day_distinct_blobs_same_payload_idempotent(tmp_path: Path) -> None:
    first = run(make(tmp_path, Server())[0])[0]
    restated = PAGE1.replace(b"28.34", b"28.35").replace(b'"total": "6"', b'"total": "3"')
    src, _, store = make(tmp_path, lambda _: httpx.Response(200, content=restated))
    (second,) = run(src)
    assert first.day == second.day
    assert raw_blob_path(first) != raw_blob_path(second)
    paths = store.list_paths("raw/")
    assert raw_blob_path(first) in paths
    assert raw_blob_path(second) in paths
    run(src)  # same payload again: no new blob
    assert store.list_paths("raw/") == paths


def echoing_server(key: str) -> Handler:
    inner = Server()

    def h(request: httpx.Request) -> httpx.Response:
        body = json.loads(inner(request).content)
        body["request"]["params"]["api_key"] = request.url.params["api_key"]
        body["request"]["params"]["facets"]["x"] = [{"api_key": key}]
        return httpx.Response(200, content=json.dumps(body, indent=2).encode())

    return h


def test_key_echo_stripped_before_digest_and_storage(tmp_path: Path) -> None:
    def fetch_with(key: str, sub: str) -> tuple[list[RawChunk], LocalStore]:
        store = LocalStore(tmp_path / sub)
        src = EiaSource(
            httpx.Client(transport=httpx.MockTransport(echoing_server(key))),
            SecretStr(key),
            store,
            np.random.Generator(np.random.PCG64(1)),
            clock=FixedClock(1),
            sleep=lambda _: None,
        )
        return run(src), store

    chunks_a, store_a = fetch_with("KEYAAAAAAAA", "a")
    chunks_b, _ = fetch_with("KEYBBBBBBBB", "b")
    assert [c.sha256 for c in chunks_a] == [c.sha256 for c in chunks_b]  # rotation-invariant
    for c in chunks_a:
        assert b"KEYAAAAAAAA" not in c.payload
        assert c.sha256 == hashlib.sha256(c.payload).hexdigest()
        assert "api_key" not in json.loads(c.payload)["request"]["params"]
        assert json.loads(c.payload)["response"]["data"]  # data untouched
    for path in store_a.list_paths("raw/"):
        assert b"KEYAAAAAAAA" not in gzip.decompress((tmp_path / "a" / path).read_bytes())


def test_strip_credential_echo_is_verbatim_without_echo() -> None:
    assert strip_credential_echo(PAGE1) == PAGE1
    assert strip_credential_echo(b"not json") == b"not json"
    assert strip_api_key([1]) == [1]


def test_key_outside_request_refused(tmp_path: Path) -> None:
    leak = PAGE1.replace(b'"RWTC"', b'"RWTC" ', 1).replace(b"WTI Crude Oil", KEY.encode(), 1)
    src, _, store = make(tmp_path, lambda _: httpx.Response(200, content=leak))
    with pytest.raises(EiaUnavailable):
        run(src)
    assert store.list_paths("raw/") == []


def test_retry_after_capped_at_300(tmp_path: Path) -> None:
    server = Server([httpx.Response(429, headers={"Retry-After": "120"})])
    src, sleeps, _ = make(tmp_path, server)
    run(src)
    assert sleeps == [120.0]
    server = Server([httpx.Response(429, headers={"Retry-After": "9999"})])
    src, sleeps, _ = make(tmp_path / "2", server)
    run(src)
    assert sleeps == [base.MAX_RETRY_AFTER_S]


def test_strip_scopes_eia_request_and_fred_observations() -> None:
    body = {"request": {"params": {"api_key": "k"}}, "response": {"api_key": "keep"}}
    assert strip_api_key(body, only="request") == {
        "request": {"params": {}},
        "response": {"api_key": "keep"},
    }
    fred_body = {"api_key": "k", "meta": [{"API_KEY": "k"}], "observations": [{"api_key": "d"}]}
    assert strip_api_key(fred_body, keep="observations") == {
        "meta": [{}],
        "observations": [{"api_key": "d"}],
    }
    assert strip_api_key({"x": 1}, only="request") == {"x": 1}


def test_redact_url_is_case_insensitive_and_shared_by_both_sources() -> None:
    assert vars(eia)["redact_url"] is vars(fred)["redact_url"] is base.redact_url
    assert redact_url("u?API_KEY=abc'") == "u?API_KEY=**********'"
