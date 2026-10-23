"""In-memory ``BacktestStore`` with the conditional-update semantics of ``ops.backtest_job``."""

from dataclasses import replace
from datetime import datetime

from tidal.api.aggregator import ChildCounts, JobRecord
from tidal.contracts.models import JobStatus

_TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.PARTIAL}


class FakeBacktestStore:
    """Dict-backed store; ``fail_next`` makes the next call raise (transient SQL error)."""

    def __init__(self) -> None:
        """Create an empty store."""
        self.rows: dict[str, JobRecord] = {}
        self.fail_next = False

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("fake sql down")

    async def insert(self, records: list[JobRecord]) -> None:
        """Insert all rows."""
        self._maybe_fail()
        for r in records:
            self.rows[r.job_id] = r

    async def get(self, job_id: str) -> JobRecord | None:
        """The row or None."""
        self._maybe_fail()
        return self.rows.get(job_id)

    def children(self, parent: str) -> list[JobRecord]:
        """Children of ``parent`` ordered by index."""
        kids = [r for r in self.rows.values() if r.parent_job_id == parent]
        return sorted(kids, key=lambda r: r.child_index or 0)

    async def child_counts(self, parent_job_id: str) -> ChildCounts:
        """Counts by status."""
        kids = self.children(parent_job_id)
        done = [k.finished_at for k in kids if k.finished_at]
        return ChildCounts(
            total=len(kids),
            succeeded=sum(k.status == JobStatus.SUCCEEDED for k in kids),
            failed=sum(k.status == JobStatus.FAILED for k in kids),
            last_finished=max(done) if done else None,
        )

    async def finish_child(
        self,
        job_id: str,
        *,
        status: JobStatus,
        metrics_json: str | None,
        result_sha256: str | None,
        error_code: str | None,
        finished_at: datetime,
    ) -> bool:
        """Conditional update: only a non-terminal row changes."""
        self._maybe_fail()
        row = self.rows[job_id]
        if row.status in _TERMINAL:
            return False
        self.rows[job_id] = replace(
            row, status=status, metrics_json=metrics_json, result_sha256=result_sha256,
            error_code=error_code, finished_at=finished_at,
        )  # fmt: skip
        return True

    async def update_parent(
        self, job_id: str, *, status: JobStatus, succeeded: int, failed: int,
        finished_at: datetime | None,
    ) -> None:  # fmt: skip
        """Set status; keep an existing ``finished_at``."""
        row = self.rows[job_id]
        self.rows[job_id] = replace(row, status=status, finished_at=row.finished_at or finished_at)

    async def list_parents(
        self, owner_hash: str, status: JobStatus | None, offset: int, limit: int
    ) -> list[JobRecord]:
        """Owner's top-level jobs, newest (highest id) first."""
        rows = [
            r for r in self.rows.values()
            if r.parent_job_id is None and r.owner_hash == owner_hash
            and (status is None or r.status == status)
        ]  # fmt: skip
        rows.sort(key=lambda r: r.job_id, reverse=True)
        return rows[offset : offset + limit]
