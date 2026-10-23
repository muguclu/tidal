"""In-memory ``BacktestStore`` with the conditional-update semantics of ``ops.backtest_job``."""

from dataclasses import replace
from datetime import datetime

from tidal.api.aggregator import ENQUEUE_FAILED, ChildCounts, JobRecord
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
        """Conditional update: non-terminal rows, or a failed ``ENQUEUE_FAILED`` row, change."""
        self._maybe_fail()
        row = self.rows[job_id]
        overridable = row.status == JobStatus.FAILED and row.error_code == ENQUEUE_FAILED
        if row.status in _TERMINAL and not overridable:
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
        """Monotonic: only if the done count does not go backwards; keep ``finished_at``."""
        self._maybe_fail()
        row = self.rows[job_id]
        stored = (row.children_succeeded or 0) + (row.children_failed or 0)
        if succeeded + failed < stored:
            return
        self.rows[job_id] = replace(
            row, status=status, children_succeeded=succeeded, children_failed=failed,
            finished_at=row.finished_at or finished_at,
        )  # fmt: skip

    async def list_parents(
        self, owner_hash: str, status: JobStatus | None, before_job_id: str | None, limit: int
    ) -> list[JobRecord]:
        """Owner's top-level jobs with ``job_id < before_job_id``, newest first."""
        rows = [
            r for r in self.rows.values()
            if r.parent_job_id is None and r.owner_hash == owner_hash
            and (status is None or r.status == status)
            and (before_job_id is None or r.job_id < before_job_id)
        ]  # fmt: skip
        rows.sort(key=lambda r: r.job_id, reverse=True)
        return rows[:limit]

    async def stale_children(self, job_id: str, submitted_before: datetime) -> list[str]:
        """Non-terminal children (or the leaf itself) submitted before the cutoff."""
        row = self.rows[job_id]
        pool = self.children(job_id) if row.children_total is not None else [row]
        return [
            r.job_id for r in pool
            if r.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            and r.submitted_at < submitted_before
        ]  # fmt: skip
