"""Out-of-process probe: stream N rows through tidal-api -> RemoteReader -> tidal-query.

Run by ``test_stream_memory.py`` in a fresh interpreter so the peak is that of the services and
not of the pytest session. Everything runs in this one process over real loopback sockets
(``httpx.ASGITransport`` buffers whole bodies, so it cannot show streaming memory):

    client --HTTP--> tidal-api (uvicorn) --HTTP--> tidal-query (uvicorn) --> synthetic reader

Prints one JSON line: ``rows``, ``bytes``, ``rss_peak_mb``, ``arrow_max_mb``.
The client writes the body to disk, then counts rows batch by batch (also memory-bounded).
"""

import asyncio
import ctypes
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import numpy as np
import pyarrow as pa
import uvicorn

from tidal.api.app import create_app
from tidal.common.config import Settings
from tidal.contracts.models import Tier
from tidal.contracts.schemas import TICK_V1
from tidal.query.protocols import Reader, RowStream
from tidal.query.service import (
    AnonymousTokenProvider,
    FailClosedValidator,
    create_query_app,
    remote_reader_from_settings,
)

BATCH = 65_536
T0 = 1_585_699_200 * 10**9  # 2020-04-01
SPAN = 31 * 86_400 * 10**9  # the 31-day tick limit


def peak_rss_bytes() -> int:
    """Peak working set of this process (Windows: PeakWorkingSetSize; POSIX: ru_maxrss)."""
    if os.name == "nt":
        from ctypes import wintypes  # noqa: PLC0415

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]  # fmt: skip

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        windll: Any = ctypes.windll  # type: ignore[attr-defined,unused-ignore]
        proc = windll.kernel32.GetCurrentProcess()
        ok = windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(proc), ctypes.byref(counters), counters.cb
        )
        if not ok:
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)
    resource: Any = importlib.import_module("resource")  # POSIX only; Any for Windows type-check
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB


class SyntheticReader:
    """Generates ``rows`` ticks (negative prices included) batch by batch; holds nothing."""

    def __init__(self, rows: int) -> None:
        self.rows = rows

    async def stream_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> RowStream:
        step = SPAN // self.rows

        async def batches() -> AsyncIterator[pa.RecordBatch]:
            for lo in range(0, self.rows, BATCH):
                n = min(BATCH, self.rows - lo)
                i = np.arange(lo, lo + n, dtype=np.int64)
                ts = start_ns + i * step
                yield pa.RecordBatch.from_arrays(
                    [
                        pa.array(ts),
                        pa.array([instrument_id] * n)
                        .dictionary_encode()
                        .cast(TICK_V1.field(1).type),
                        pa.array((i % 2000).astype(np.float64) - 1000.0),  # negatives are valid
                        pa.array(np.ones(n, dtype=np.float32)),
                        pa.array(np.zeros(n, dtype=np.int8)),
                        pa.array(np.zeros(n, dtype=np.int8)),
                        pa.array(ts),
                        pa.array(np.ones(n, dtype=np.int16)),
                    ],
                    schema=TICK_V1,
                )
                await asyncio.sleep(0)

        return RowStream(Tier.HOT, batches())

    async def ping(self) -> None:
        return None


def serve(app: Any, port_box: list[int]) -> tuple[uvicorn.Server, threading.Thread]:
    """Run ``app`` in a uvicorn thread on a free loopback port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port_box.append(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.05)
    return server, thread


async def run(rows: int, fmt: str) -> dict[str, float]:
    env = {"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"}
    q_port: list[int] = []
    query_app = create_query_app(
        cast("Reader", SyntheticReader(rows)), FailClosedValidator(), Settings.from_env(env)
    )
    q_server, q_thread = serve(query_app, q_port)
    settings = Settings.from_env({**env, "TIDAL_QUERY__BASE_URL": f"http://127.0.0.1:{q_port[0]}"})
    remote = remote_reader_from_settings(settings, tokens=AnonymousTokenProvider())
    api_port: list[int] = []
    api_server, api_thread = serve(create_app(settings, reader=remote), api_port)

    params = {
        "instrument_id": "EIA.WTI.SPOT",
        "start": str(T0),
        "end": str(T0 + SPAN),
        "format": fmt,
    }
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "body"
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port[0]}", timeout=600) as client,
            client.stream("GET", "/v1/ticks", params=params) as resp,
        ):
            resp.raise_for_status()
            with path.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    fh.write(chunk)
        peak = peak_rss_bytes()
        arrow_max = pa.default_memory_pool().max_memory() or 0
        counted = 0
        if fmt == "arrow":
            with path.open("rb") as src, pa.ipc.open_stream(src) as rdr:
                counted = sum(b.num_rows for b in rdr)
        else:
            import pyarrow.parquet as pq  # noqa: PLC0415

            counted = pq.ParquetFile(path).metadata.num_rows
    api_server.should_exit = q_server.should_exit = True
    api_thread.join(10)
    q_thread.join(10)
    assert counted == rows, (counted, rows)
    return {
        "rows": counted, "bytes": total,
        "rss_peak_mb": peak / 2**20, "arrow_max_mb": arrow_max / 2**20,
    }  # fmt: skip


if __name__ == "__main__":
    out = asyncio.run(run(int(sys.argv[1]), sys.argv[2]))
    print(json.dumps(out))  # noqa: T201
