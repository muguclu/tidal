"""Fault injection for the tiering crash tests (T-206): fault points, a runner and a worker script.

The step order of the migration and of the compaction is the contract in the ``tiering.py`` /
``compact.py`` module docstrings. A *fault point* is a deterministic place in that order, reached by
wrapping the public step functions of ``tidal.storage`` (no source edit) or the blob store.

Modes: ``kill`` ends the process the hard way (SIGKILL; TerminateProcess on Windows, so no
``finally`` clause runs and the SQL session dies with the process), ``raise`` raises :class:`Boom`
(a ``BaseException``, for in-process property tests), ``hang`` announces the point and waits to be
killed from outside (lock tests).

Run as a script (``python crash_support.py``) it is the worker the tests start as a subprocess; all
parameters travel in ``CRASH_*`` environment variables, the result is one JSON line on stdout.
"""

import functools
import json
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from unittest import mock

from azure.storage.blob import BlobServiceClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from tidal.common.timeutil import FixedClock
from tidal.storage import compact as compact_mod
from tidal.storage import sql_repo as sr
from tidal.storage import tiering as tiering_mod
from tidal.storage.compact import compact
from tidal.storage.parquet_writer import AzureBlobStore, write_partition
from tidal.storage.retag import AzureTagger
from tidal.storage.tiering import TieringConfig, run_tiering

Hook = tuple[Any, str, str]  # (module or class, attribute, "before" | "after")

#: Migration fault points in step order (tiering.py module docstring).
MIGRATE_POINTS: Final = (
    "before_write",  # 3 not started
    "after_part_put",  # 3 inside: a part is in Blob, the next one is not
    "after_write",  # 3 done, 4 (verify) not started
    "before_tag",  # 4 done
    "before_manifest",  # 5 done
    "manifest_after_delete",  # 6 between the manifest delete and the put
    "before_delete",  # 6 done, nothing deleted from SQL
    "between_delete_chunks",  # 7 a chunk is gone, the next one is not
    "before_watermark",  # 7 done
    "after_watermark",  # 8 done, lock still held
)
#: Compaction fault points in step order (compact.py module docstring).
COMPACT_POINTS: Final = (
    "before_write",
    "after_part_put",
    "after_write",
    "before_tag",
    "before_delete",
    "between_delete_chunks",
    "before_manifest",
    "manifest_after_delete",
)

_MIGRATE_HOOKS: Final[dict[str, Hook]] = {
    "before_write": (tiering_mod, "write_partition", "before"),
    "after_write": (tiering_mod, "write_partition", "after"),
    "before_tag": (tiering_mod, "tag_partition", "before"),
    "before_manifest": (tiering_mod, "update_manifest", "before"),
    "before_delete": (tiering_mod, "delete_chunk", "before"),
    "between_delete_chunks": (tiering_mod, "delete_chunk", "after"),
    "before_watermark": (sr.SqlRepository, "set_watermark", "before"),
    "after_watermark": (sr.SqlRepository, "set_watermark", "after"),
}
_COMPACT_HOOKS: Final[dict[str, Hook]] = {
    "before_write": (compact_mod, "write_partition", "before"),
    "after_write": (compact_mod, "write_partition", "after"),
    "before_tag": (compact_mod, "ensure_tags", "before"),
    "before_delete": (compact_mod, "delete_part", "before"),
    "between_delete_chunks": (compact_mod, "delete_part", "after"),
    "before_manifest": (compact_mod, "update_manifest", "before"),
}
_STORE_POINTS: Final = ("after_part_put", "manifest_after_delete")


class Boom(BaseException):
    """Raised at a fault point in ``raise`` mode (a dead process, not a ``TidalError``)."""


@dataclass(frozen=True, slots=True)
class Fault:
    """Fire at the ``nth`` (1-based) time the step ``point`` is reached."""

    point: str
    nth: int = 1
    mode: str = "kill"  # kill | raise | hang
    mark: Path | None = None  # written just before firing: proves the point was reached


@dataclass(frozen=True, slots=True)
class Params:
    """Everything one tiering/compaction run needs besides its connections."""

    op: str  # migrate | compact
    now_ns: int
    hot_window_days: int
    batch_rows: int = 500_000
    delete_chunk_rows: int = 50_000
    max_part_rows: int = 10_000_000
    tables: tuple[str, ...] = ("bar_hot", "tick_hot")
    datasets: tuple[str, ...] = ("bars_1d", "bars_1h", "ticks")


def _fire(fault: Fault) -> None:
    if fault.mark is not None:
        fault.mark.write_text(fault.point, encoding="ascii")
    if fault.mode == "raise":
        raise Boom(fault.point)
    if fault.mode == "hang":
        threading.Event().wait(600)  # the test kills this process from outside
        os._exit(3)
    os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))
    os._exit(137)  # unreachable on POSIX; TerminateProcess is asynchronous enough on Windows


class _Counter:
    def __init__(self, fault: Fault) -> None:
        self.fault, self.seen = fault, 0

    def __call__(self) -> None:
        self.seen += 1
        if self.seen == self.fault.nth:
            _fire(self.fault)


def _wrap(fn: Callable[..., Any], trip: _Counter, when: str) -> Callable[..., Any]:
    """Call ``trip`` before or after ``fn`` (``Any``: it wraps functions of every signature)."""

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        if when == "before":
            trip()
        out = fn(*args, **kwargs)
        if when == "after":
            trip()
        return out

    return inner


class FaultStore(AzureBlobStore):
    """Azure store that reports finished part uploads and manifest deletes to a counter."""

    def __init__(
        self,
        service: BlobServiceClient,
        on_part: Callable[[], None] | None = None,
        on_manifest_delete: Callable[[], None] | None = None,
    ) -> None:
        """Wrap ``service``; the callbacks may kill the process."""
        super().__init__(service)
        self._on_part, self._on_mdel = on_part, on_manifest_delete

    def put_new(self, path: str, local_file: Path) -> None:
        """Upload; a finished part counts as ``after_part_put``."""
        super().put_new(path, local_file)
        if self._on_part is not None and path.endswith(".parquet"):
            self._on_part()

    def delete(self, path: str) -> None:
        """Delete; a deleted manifest counts as ``manifest_after_delete``."""
        super().delete(path)
        if self._on_mdel is not None and path.endswith("_manifest.json"):
            self._on_mdel()


def _limited_write(fn: Callable[..., Any], max_part_rows: int) -> Callable[..., Any]:
    """``write_partition`` with a small ``max_part_rows`` (the migration passes none)."""

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("max_part_rows", max_part_rows)
        return fn(*args, **kwargs)

    return inner


@contextmanager
def injected(
    service: BlobServiceClient, params: Params, fault: Fault | None
) -> Iterator[FaultStore]:
    """Patch the step functions for ``fault`` and yield the store to run with."""
    hooks = _MIGRATE_HOOKS if params.op == "migrate" else _COMPACT_HOOKS
    if fault is not None and fault.point not in (*hooks, *_STORE_POINTS):
        raise ValueError(f"unknown fault point {fault.point!r} for {params.op}")
    trip = _Counter(fault) if fault is not None else None
    with ExitStack() as stack:
        if params.op == "migrate":
            stack.enter_context(
                mock.patch.object(
                    tiering_mod,
                    "write_partition",
                    _limited_write(write_partition, params.max_part_rows),
                )
            )
        if fault is not None and trip is not None and fault.point in hooks:
            target, attr, when = hooks[fault.point]
            stack.enter_context(
                mock.patch.object(target, attr, _wrap(getattr(target, attr), trip, when))
            )
            # the patch above replaced the module attribute; after_write/before_write share it
        yield FaultStore(
            service,
            trip if fault and fault.point == "after_part_put" else None,
            trip if fault and fault.point == "manifest_after_delete" else None,
        )


def run_once(
    engine: Engine, service: BlobServiceClient, params: Params, fault: Fault | None = None
) -> dict[str, Any]:  # JSON-shaped summary
    """One migration or compaction run (all tables / datasets) with ``fault`` armed."""
    clock = FixedClock(params.now_ns)
    out: dict[str, Any] = {"locked": False, "errors": []}
    with injected(service, params, fault) as store:
        tagger = AzureTagger(service)
        if params.op == "migrate":
            cfg = TieringConfig(params.hot_window_days, params.batch_rows, params.delete_chunk_rows)
            for res in run_tiering(engine, store, tagger, clock, cfg, params.tables):
                out["locked"] = out["locked"] or res.locked
                out["errors"] += res.errors
                out[res.table] = res.rows_moved
        else:
            for dataset in params.datasets:
                cres = compact(
                    engine, store, tagger, clock, dataset, max_part_rows=params.max_part_rows
                )
                out["locked"] = out["locked"] or cres.locked
                out["errors"] += cres.errors
    return out


def make_engine(odbc: str) -> Engine:
    """SQLAlchemy engine for a raw ODBC connection string."""
    return create_engine(URL.create("mssql+pyodbc", query={"odbc_connect": odbc}))


def _worker() -> None:
    env = os.environ
    params = Params(
        op=env["CRASH_OP"],
        now_ns=int(env["CRASH_NOW_NS"]),
        hot_window_days=int(env["CRASH_HOT_DAYS"]),
        batch_rows=int(env["CRASH_BATCH_ROWS"]),
        delete_chunk_rows=int(env["CRASH_CHUNK_ROWS"]),
        max_part_rows=int(env["CRASH_MAX_PART_ROWS"]),
    )
    point = env.get("CRASH_POINT")
    fault = (
        Fault(point, int(env.get("CRASH_NTH", "1")), env.get("CRASH_MODE", "kill"),
              Path(env["CRASH_MARK"]))
        if point
        else None
    )  # fmt: skip
    engine = make_engine(env["CRASH_ODBC"])
    service = BlobServiceClient.from_connection_string(env["CRASH_AZURITE"])
    print(json.dumps(run_once(engine, service, params, fault)), flush=True)  # noqa: T201


if __name__ == "__main__":
    _worker()
    sys.exit(0)
