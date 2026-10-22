import gzip
import hashlib
import json
import math
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
from tidal.ingest.sources import base, fred
from tidal.ingest.sources.base import RawChunk, raw_blob_path, redact_url
from tidal.ingest.sources.fred import (
    FredRequestRejected,
    FredSource,
    FredUnavailable,
    parse_fred_page,
    strip_api_key,
    strip_credential_echo,
)
from tidal.storage.parquet_writer import LocalStore

FIX = Path(__file__).parents[2] / "fixtures" / "fred"
PAGE1, PAGE2 = (FIX / "dexuseu_page1.json").read_bytes(), (FIX / "dexuseu_page2.json").read_bytes()
KEY = "SUPERSECRETKEY123"
START, END = date(1999, 1, 4), date(1999, 1, 11)
IID = "FRED.EURUSD.FX"
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
    tmp_path: Path, server: Handler, max_pages: int = fred.MAX_PAGES, key: str = KEY
) -> tuple[FredSource, list[float], LocalStore]:
    sleeps: list[float] = []
    store = LocalStore(tmp_path)
    src = FredSource(
        httpx.Client(transport=httpx.MockTransport(server)),
        SecretStr(key),
        store,
        np.random.Generator(np.random.PCG64(1)),
        clock=FixedClock(123),
        sleep=sleeps.append,
        max_pages=max_pages,
    )
    return src, sleeps, store


def run(src: FredSource) -> list[RawChunk]:
    return list(src.fetch(IID, START, END))


def test_pagination_two_pages_saved(tmp_path: Path) -> None:
    server = Server()
    src, _, store = make(tmp_path, server)
    chunks = run(src)
    assert [c.page for c in chunks] == [0, 1]
    assert [c.payload for c in chunks] == [PAGE1, PAGE2]
    assert all(c.source == "FRED" and c.series == "DEXUSEU" for c in chunks)
    assert [int(r.url.params["offset"]) for r in server.requests] == [0, 3]
    q = server.requests[0].url
    assert q.path == "/fred/series/observations"
    assert q.params["series_id"] == "DEXUSEU"
    assert q.params["file_type"] == "json"
    assert q.params["limit"] == "100000"
    assert (q.params["observation_start"], q.params["observation_end"]) == (
        "1999-01-04",
        "1999-01-11",
    )
    paths = [raw_blob_path(c) for c in chunks]
    assert paths[0].startswith("raw/fred/dexuseu/19990104-")
    assert paths[1].startswith("raw/fred/dexuseu/19990107-")
    assert paths[0].endswith(".json.gz")
    assert store.list_paths("raw/fred/") == sorted(paths)
    assert gzip.decompress((tmp_path / paths[0]).read_bytes()) == PAGE1


def test_saved_before_first_chunk_is_yielded(tmp_path: Path) -> None:
    src, _, _ = make(tmp_path, Server())
    first = next(src.fetch(IID, START, END))
    assert (tmp_path / raw_blob_path(first)).exists()


def test_raw_saved_even_if_parse_fails(tmp_path: Path) -> None:
    bad = b'{"count":1,"observations":[{"date":"1999-01-04","value":"x"}]}'
    src, _, store = make(tmp_path, lambda _: httpx.Response(200, content=bad))
    (chunk,) = run(src)
    assert store.list_paths("raw/fred/") == [raw_blob_path(chunk)]
    v = parse_fred_page(chunk.payload, "DEXUSEU").column("value").to_numpy().tolist()
    assert math.isnan(v[0])


def test_refetch_is_idempotent(tmp_path: Path) -> None:
    src, _, store = make(tmp_path, Server())
    run(src)
    before = {p: (tmp_path / p).read_bytes() for p in store.list_paths("raw/")}
    run(src)
    assert {p: (tmp_path / p).read_bytes() for p in store.list_paths("raw/")} == before


def test_changed_payload_same_day_gets_distinct_blob(tmp_path: Path) -> None:
    first = run(make(tmp_path, Server())[0])[0]
    restated = PAGE1.replace(b"1.1812", b"1.1813").replace(b'"count": 6', b'"count": 3')
    src, _, store = make(tmp_path, lambda _: httpx.Response(200, content=restated))
    (second,) = run(src)
    assert first.day == second.day
    assert raw_blob_path(first) != raw_blob_path(second)
    assert {raw_blob_path(first), raw_blob_path(second)} <= set(store.list_paths("raw/"))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_then_success(tmp_path: Path, status: int) -> None:
    src, sleeps, _ = make(tmp_path, Server([httpx.Response(status), httpx.Response(status)]))
    assert len(run(src)) == 2
    assert 0.5 <= sleeps[0] <= 1.0
    assert 1.0 <= sleeps[1] <= 2.0


def test_retry_after_lower_bound_and_cap(tmp_path: Path) -> None:
    server = Server([httpx.Response(429, headers={"Retry-After": "7"})])
    src, sleeps, _ = make(tmp_path, server)
    run(src)
    assert sleeps == [7.0]
    server = Server([httpx.Response(429, headers={"Retry-After": "9999"})])
    src, sleeps, _ = make(tmp_path / "2", server)
    run(src)
    assert sleeps == [base.MAX_RETRY_AFTER_S]


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
    with pytest.raises(FredUnavailable) as ei:
        run(src)
    assert len(server.requests) == base.MAX_ATTEMPTS
    assert len(sleeps) == base.MAX_ATTEMPTS - 1
    assert KEY not in str(ei.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_4xx_fails_fast_without_leaking_key(tmp_path: Path, status: int) -> None:
    server = Server([httpx.Response(status)])
    src, sleeps, _ = make(tmp_path, server)
    with structlog.testing.capture_logs() as logs, pytest.raises(FredRequestRejected) as ei:
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
        FredSource(httpx.Client(), None, LocalStore(tmp_path), np.random.default_rng(0))
    with pytest.raises(ConfigError):
        FredSource(httpx.Client(), SecretStr(""), LocalStore(tmp_path), np.random.default_rng(0))


def test_max_pages_cap(tmp_path: Path) -> None:
    endless = PAGE1.replace(b'"count": 6', b'"count": 999999')
    src, _, _ = make(tmp_path, lambda _: httpx.Response(200, content=endless), max_pages=3)
    it = src.fetch(IID, START, END)
    assert [next(it).page for _ in range(3)] == [0, 1, 2]
    with pytest.raises(FredUnavailable):
        next(it)


def test_malformed_page_rejected(tmp_path: Path) -> None:
    src, _, _ = make(tmp_path, lambda _: httpx.Response(200, content=b"<html>"))
    with pytest.raises(FredUnavailable):
        run(src)
    with pytest.raises(FredUnavailable):
        parse_fred_page(b"nope", "DEXUSEU")


def test_empty_page_stops(tmp_path: Path) -> None:
    empty = b'{"count":0,"observations":[]}'
    src, _, _ = make(tmp_path, lambda _: httpx.Response(200, content=empty))
    assert len(run(src)) == 1
    assert parse_fred_page(empty, "DEXUSEU").num_rows == 0


def test_parse_golden_start_and_adr0009() -> None:
    t = parse_fred_page(PAGE1, "DEXUSEU")
    assert t.column_names == ["ts_ns", "value", "series"]
    assert str(t.schema.field("ts_ns").type) == "int64"
    assert str(t.schema.field("value").type) == "double"
    ts = t.column("ts_ns").to_numpy().tolist()
    assert ts[0] == 915_408_000 * 10**9  # 1999-01-04T00:00:00Z (ADR-0009)
    assert all(x % NS_PER_DAY == 0 for x in ts)
    assert ts == sorted(ts)
    assert t.column("value").to_pylist()[0] == 1.1812
    assert set(t.column("series").to_pylist()) == {"DEXUSEU"}


def test_missing_marker_dot_becomes_nan() -> None:
    v = parse_fred_page(PAGE2, "DEXUSEU").column("value").to_numpy().tolist()
    assert v[0] == 1.1672
    assert math.isnan(v[1])  # "." -> NaN; T-105 must drop these rows
    assert v[2] == 1.1554


def test_negative_values_kept() -> None:
    body = b'{"count":1,"observations":[{"date":"2020-04-20","value":"-37.63"}]}'
    assert parse_fred_page(body, "X").column("value").to_pylist() == [-37.63]


def echoing_server(key: str) -> Handler:
    inner = Server()

    def h(request: httpx.Request) -> httpx.Response:
        body = json.loads(inner(request).content)
        body["api_key"] = request.url.params["api_key"]
        body["request"] = {"params": [{"api_key": key}]}
        return httpx.Response(200, content=json.dumps(body, indent=2).encode())

    return h


def test_key_echo_stripped_before_digest_and_storage(tmp_path: Path) -> None:
    def fetch_with(key: str, sub: str) -> tuple[list[RawChunk], LocalStore]:
        src, _, store = make(tmp_path / sub, echoing_server(key), key=key)
        return run(src), store

    chunks_a, store_a = fetch_with("KEYAAAAAAAA", "a")
    chunks_b, _ = fetch_with("KEYBBBBBBBB", "b")
    assert [c.sha256 for c in chunks_a] == [c.sha256 for c in chunks_b]  # rotation-invariant
    for c in chunks_a:
        assert b"KEYAAAAAAAA" not in c.payload
        assert c.sha256 == hashlib.sha256(c.payload).hexdigest()
        assert "api_key" not in json.loads(c.payload)
        assert json.loads(c.payload)["observations"]  # data untouched
    for path in store_a.list_paths("raw/"):
        assert b"KEYAAAAAAAA" not in gzip.decompress((tmp_path / "a" / path).read_bytes())


def test_strip_credential_echo_is_verbatim_without_echo() -> None:
    assert strip_credential_echo(PAGE1) == PAGE1
    assert strip_credential_echo(b"not json") == b"not json"
    assert strip_api_key([1]) == [1]


def test_key_elsewhere_refused(tmp_path: Path) -> None:
    leak = PAGE1.replace(b'"asc"', KEY.encode(), 1)
    src, _, store = make(tmp_path, lambda _: httpx.Response(200, content=leak))
    with pytest.raises(FredUnavailable):
        run(src)
    assert store.list_paths("raw/") == []


def test_3xx_is_not_success(tmp_path: Path) -> None:
    server = Server([httpx.Response(302, headers={"Location": "http://x"})])
    src, sleeps, _ = make(tmp_path, server)
    with pytest.raises(FredUnavailable, match="302"):
        run(src)
    assert sleeps == []


def test_bad_date_is_unavailable() -> None:
    body = b'{"count":1,"observations":[{"date":"not-a-date","value":"1"}]}'
    with pytest.raises(FredUnavailable):
        parse_fred_page(body, "DEXUSEU")
