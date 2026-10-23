"""Observed tier of JSON pages through RemoteReader (T-210 review): a late SQL restatement below
the watermark makes the page ``mixed`` while the up-front (header) label of a stream stays ``cold``.
"""

from pathlib import Path

import httpx
import numpy as np
import pytest

from tidal.api.app import create_app
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.query import duck
from tidal.query.reader import UnifiedReader
from tidal.query.service import (
    AnonymousTokenProvider,
    FailClosedValidator,
    create_query_app,
    remote_reader_from_settings,
)
from tidal.storage import parquet_writer as pw
from tidal.storage import sql_repo as sr
from unit.query.query_support import DAY, WTI, bars_table, instrument_row, sqlite_engine

pytestmark = pytest.mark.anyio
LOCAL = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
DAY0 = 1_587_340_800 * 10**9  # 2020-04-20


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build(tmp_path: Path, *, restated: bool) -> httpx.AsyncClient:
    """API -> RemoteReader -> query app -> UnifiedReader; cold holds Apr 19-20, watermark Apr 21."""
    store = pw.LocalStore(tmp_path)
    cold = bars_table(
        np.array([DAY0 - DAY, DAY0]), np.array([18.27, -37.63]), np.array([np.nan, 5.0]), 1
    )
    pw.write_partition(
        store, dataset="bars_1d", instrument_id=WTI, year=2020, month=0, batches=[cold],
        source_hash="x",
    )  # fmt: skip
    repo = sr.SqlRepository(sqlite_engine())
    repo.upsert_instruments([instrument_row()])
    repo.set_watermark("md.bar_hot", DAY0 + DAY)
    if restated:  # late restatement of Apr 20, written to SQL below the watermark
        repo.upsert_bars(bars_table(np.array([DAY0]), np.array([-30.0]), np.array([5.0]), 9))
    reader = UnifiedReader(repo, duck.local_session(tmp_path), FixedClock(0), LOCAL)
    query = create_query_app(reader, FailClosedValidator(), LOCAL)
    remote = remote_reader_from_settings(
        LOCAL, tokens=AnonymousTokenProvider(), transport=httpx.ASGITransport(app=query)
    )
    api = create_app(LOCAL, reader=remote)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")


PARAMS = {
    "instrument_id": WTI, "interval": "1d", "start": str(DAY0 - DAY), "end": str(DAY0 + DAY),
}  # fmt: skip


@pytest.mark.parametrize(("restated", "json_tier"), [(True, "mixed"), (False, "cold")])
async def test_json_page_reports_the_observed_tier(
    tmp_path: Path, restated: bool, json_tier: str
) -> None:
    async with build(tmp_path, restated=restated) as client:
        r = await client.get("/v1/bars", params=PARAMS)
        assert r.status_code == 200
        assert r.headers["X-Tidal-Tier"] == json_tier
        assert r.json()["tier"] == json_tier
        # arrow keeps the label of the requested range (sent before any data)
        a = await client.get("/v1/bars", params={**PARAMS, "format": "arrow"})
        assert a.headers["X-Tidal-Tier"] == "cold"
