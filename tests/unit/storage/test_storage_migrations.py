"""Migration file == data-contracts §6, plus runner behaviour against a recording fake engine."""

# ruff: noqa: PT018, PLC0415, PLW0108

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tidal.storage import sql_repo

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "sql" / "migrations"
CONTRACT = ROOT / "docs" / "contracts" / "data-contracts.md"


def _split_top_level(body: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in body:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [*parts, cur]


def parse_tables(sql: str) -> dict[str, set[str]]:
    """Map ``schema.table`` -> column names from every ``CREATE TABLE`` in ``sql``."""
    sql = re.sub(r"--[^\n]*", "", sql)
    tables = {}
    for m in re.finditer(r"CREATE TABLE ([\w.]+)\s*\(", sql):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(sql[i], 0)
            i += 1
        cols = set()
        for item in _split_top_level(sql[m.end() : i - 1]):
            first = item.split()[0].strip("[]").lower() if item.split() else ""
            if first and first not in {"constraint", "primary", "unique"}:
                cols.add(first)
        tables[m.group(1)] = cols
    return tables


def contract_sql() -> str:
    section = CONTRACT.read_text(encoding="utf-8").split("## 6. Hot-store SQL tables")[1]
    return re.search(r"```sql\n(.*?)```", section, re.S).group(1)  # type: ignore[union-attr]


def test_migration_matches_contract_section_6() -> None:
    contract = parse_tables(contract_sql())
    migration = parse_tables((MIGRATIONS / "0001_init.sql").read_text(encoding="utf-8"))
    assert set(contract) == {
        "ref.instrument",
        "md.bar_hot",
        "md.tick_hot",
        "ops.migration_watermark",
        "ops.ingest_checkpoint",
        "ops.backtest_job",
    }
    assert set(migration) == set(contract) | {"md.bar_baseline"}
    for table, cols in contract.items():
        assert migration[table] == cols, table
    # baseline: bar_hot with a DATETIME2 timestamp in place of ts_ns
    assert migration["md.bar_baseline"] == (migration["md.bar_hot"] - {"ts_ns"}) | {"ts"}


def test_migration_keeps_contract_options() -> None:
    text = (MIGRATIONS / "0001_init.sql").read_text(encoding="utf-8")
    assert text.count("DATA_COMPRESSION = PAGE") == 2
    assert "DATA_COMPRESSION = NONE" in text and "DATETIME2(7)" in text


def test_sqlalchemy_tables_match_migration() -> None:
    migration = parse_tables((MIGRATIONS / "0001_init.sql").read_text(encoding="utf-8"))
    for table in sql_repo.metadata.tables.values():
        cols = {c.name for c in table.columns}
        assert cols == migration[f"{table.schema}.{table.name}"], table.fullname


class FakeResult:
    def __init__(self, rows: Sequence[tuple[str, str]], scalar: int | None = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def scalar(self) -> int | None:
        return self._scalar

    def __iter__(self) -> Any:
        return iter(self._rows)


class FakeConn:
    def __init__(self, log: list[str], applied: dict[str, str]) -> None:
        self.log, self.applied = log, applied
        self.lock_rc = 0

    def commit(self) -> None:
        self.log.append("COMMIT")

    def exec_driver_sql(self, sql: str, params: tuple[str, str] | None = None) -> FakeResult:
        self.log.append(sql)
        if "sp_getapplock" in sql:
            return FakeResult([], scalar=self.lock_rc)
        if sql.startswith("SELECT version"):
            return FakeResult(list(self.applied.items()))
        if params:
            self.applied[params[0]] = params[1]
        return FakeResult([])

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, dialect: str = "mssql", lock_rc: int = 0) -> None:
        self.log: list[str] = []
        self.applied: dict[str, str] = {}
        self.dialect = type("D", (), {"name": dialect})()
        self.lock_rc = lock_rc

    def connect(self) -> FakeConn:
        conn = FakeConn(self.log, self.applied)
        conn.lock_rc = self.lock_rc
        return conn

    def begin(self) -> FakeConn:
        return FakeConn(self.log, self.applied)


def test_apply_is_ordered_and_idempotent(tmp_path: Path) -> None:
    (tmp_path / "0002_b.sql").write_text("SELECT 2")
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    engine = FakeEngine()
    assert sql_repo.apply_migrations(engine, tmp_path) == ["0001_a", "0002_b"]  # type: ignore[arg-type]
    ran = [s for s in engine.log if s.startswith("SELECT ") and "version" not in s]
    assert ran == ["SELECT 1", "SELECT 2"]
    assert "sp_getapplock" in engine.log[0]  # lock first, then bootstrap
    assert engine.log[1].startswith("IF SCHEMA_ID('ops')")
    before = len(engine.log)
    assert sql_repo.apply_migrations(engine, tmp_path) == []  # type: ignore[arg-type]
    assert not any(s in engine.log[before:] for s in ("SELECT 1", "SELECT 2"))


def test_edited_migration_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "0001_a.sql"
    f.write_text("SELECT 1")
    engine = FakeEngine()
    sql_repo.apply_migrations(engine, tmp_path)  # type: ignore[arg-type]
    f.write_text("SELECT 999")
    with pytest.raises(sql_repo.MigrationError):
        sql_repo.apply_migrations(engine, tmp_path)  # type: ignore[arg-type]


def test_bad_file_name(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("SELECT 1")
    with pytest.raises(sql_repo.MigrationError):
        sql_repo.discover_migrations(tmp_path)


def test_real_migration_directory_is_discoverable() -> None:
    assert [m.version for m in sql_repo.discover_migrations(sql_repo.DEFAULT_MIGRATIONS)] == [
        "0001_init"
    ]


def test_cli_migrate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    engine = FakeEngine()
    monkeypatch.setattr(sql_repo, "get_settings", lambda: object())
    monkeypatch.setattr(sql_repo, "create_engine_from_settings", lambda _s: engine)
    result = CliRunner().invoke(sql_repo.db_app, ["migrate", "--dir", str(tmp_path)])
    assert result.exit_code == 0 and "0001_a" in result.output


def test_root_cli_registers_db() -> None:
    from tidal.cli import build_app

    result = CliRunner().invoke(build_app(), ["db", "--help"])
    assert "migrate" in result.output


def test_mssql_run_is_wrapped_in_the_applock(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    engine = FakeEngine()
    sql_repo.apply_migrations(engine, tmp_path)  # type: ignore[arg-type]
    assert "tidal:migrate" in engine.log[0] and "'Session'" in engine.log[0]
    assert "sp_releaseapplock" in engine.log[-2] and engine.log[-1] == "COMMIT"


def test_lock_not_acquired_aborts_before_any_ddl(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    engine = FakeEngine(lock_rc=-1)
    with pytest.raises(sql_repo.MigrationError):
        sql_repo.apply_migrations(engine, tmp_path)  # type: ignore[arg-type]
    assert len(engine.log) == 1


def test_other_dialects_run_without_lock(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    engine = FakeEngine(dialect="sqlite")
    assert sql_repo.apply_migrations(engine, tmp_path) == ["0001_a"]  # type: ignore[arg-type]
    assert not any("applock" in s for s in engine.log)
