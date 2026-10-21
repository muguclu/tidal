"""data-contracts.md / messaging-contract.md tables == code mirror (ADR-0001)."""

import re
from pathlib import Path
from typing import get_args

import pyarrow as pa
import pytest

from tidal.contracts import messages
from tidal.contracts.instruments import (
    CATALOGUE_V1,
    INSTRUMENT_ID_RE,
    AssetClass,
    CatalogueEntry,
    Granularity,
    Instrument,
    InstrumentKind,
    RollMethod,
    Source,
)
from tidal.contracts.schemas import (
    INTERVAL_CODES,
    PARQUET_ENCODINGS,
    QUALITY_RESERVED_MASK,
    SCHEMAS,
    QualityFlag,
)

DOCS = Path(__file__).resolve().parents[2] / "docs" / "contracts"
DATA_MD = (DOCS / "data-contracts.md").read_text(encoding="utf-8")
MESSAGING_MD = (DOCS / "messaging-contract.md").read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """Body of the markdown section whose heading starts with ``heading`` (up to the next one)."""
    match = re.search(rf"^#+ {re.escape(heading)}.*?$(.*?)(?=^#+ |\Z)", text, re.M | re.S)
    assert match, heading
    return match.group(1)


def table(text: str) -> list[list[str]]:
    """Rows (without header and separator) of the first markdown table in ``text``."""
    lines = [ln for ln in text.splitlines() if ln.startswith("|")]
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    return rows[2:]


def ticks(cell: str) -> list[str]:
    """Back-ticked tokens in a cell."""
    return re.findall(r"`([^`]+)`", cell)


def arrow_type(text: str) -> pa.DataType:
    text = text.replace(" ", "")
    if m := re.fullmatch(r"dictionary<(\w+),(\w+)>", text):
        return pa.dictionary(getattr(pa, m[1])(), getattr(pa, m[2])())
    result: pa.DataType = getattr(pa, text)()
    return result


def doc_columns(heading: str) -> list[tuple[str, pa.DataType, bool, list[str]]]:
    """(name, type, nullable, row) for each column of a §2/§3 schema table."""
    out = []
    for row in table(section(DATA_MD, heading)):
        for name in ticks(row[0]):
            out.append((name, arrow_type(row[1]), "nullable" in row[-1], row))
    return out


@pytest.mark.parametrize(("heading", "schema"), [("2.", "TICK_V1"), ("3. Bar", "BAR_V1")])
def test_market_data_schemas_match_doc(heading: str, schema: str) -> None:
    doc = [(n, t, null) for n, t, null, _ in doc_columns(heading)]
    code = [(f.name, f.type, f.nullable) for f in SCHEMAS[schema]]
    assert code == doc
    assert SCHEMAS[schema].metadata == {b"tidal.schema": schema.encode()}


def test_tick_encodings_match_doc() -> None:
    doc = {n: row[2] for n, _, _, row in doc_columns("2.")}
    assert {k: str(v) for k, v in PARQUET_ENCODINGS["TICK_V1"].items()} == doc


def test_encoding_rules_from_storage_layout() -> None:
    for name, schema in SCHEMAS.items():
        enc = PARQUET_ENCODINGS[name]
        assert set(enc) <= set(schema.names)
        assert enc["ts_ns"] == "DELTA_BINARY_PACKED"
        for f in schema:
            if pa.types.is_floating(f.type):
                assert enc[f.name] == "BYTE_STREAM_SPLIT", (name, f.name)
            if pa.types.is_dictionary(f.type):
                assert enc[f.name] == "RLE_DICTIONARY", (name, f.name)


@pytest.mark.parametrize("schema", ["BT_EQUITY_V1", "BT_TRADES_V1"])
def test_backtest_schemas_match_doc(schema: str) -> None:
    match = re.search(rf"`{schema}`: `([^`]+)`", section(DATA_MD, "4."))
    assert match
    doc = [tuple(col.split()) for col in match.group(1).split(", ")]
    code = [(f.name, str(f.type).replace("double", "float64")) for f in SCHEMAS[schema]]
    assert code == [(n, str(arrow_type(t)).replace("double", "float64")) for n, t in doc]
    assert not any(f.nullable for f in SCHEMAS[schema])


def test_quality_flags_match_doc() -> None:
    rows = table(section(DATA_MD, "5."))
    named = {int(r[0]): ticks(r[1])[0] for r in rows if r[0].isdigit()}
    assert {f.bit_length() - 1: f.name for f in QualityFlag} == named
    reserved = next(r[0] for r in rows if "reserved" in r[1])
    lo, hi = (int(x) for x in re.findall(r"\d+", reserved))
    assert sum(1 << b for b in range(lo, hi + 1)) == QUALITY_RESERVED_MASK


def test_interval_codes_match_doc() -> None:
    match = re.search(r"interval_code .*-- (\S+)", section(DATA_MD, "6."))
    assert match
    doc = dict(pair.split("=") for pair in match.group(1).split(","))
    assert {str(k): v for k, v in INTERVAL_CODES.items()} == {k: int(v) for k, v in doc.items()}


ENUMS = {
    "source": Source,
    "asset_class": AssetClass,
    "kind": InstrumentKind,
    "granularity": Granularity,
    "roll_method": RollMethod,
}


def test_instrument_fields_match_doc() -> None:
    fields = Instrument.model_fields
    seen = []
    for row in table(section(DATA_MD, "1. Instrument")):
        for name in ticks(row[0]):
            seen.append(name)
            assert fields[name].is_required() == (row[2] == "no"), name
            if row[1] == "enum":
                values = ticks(row[3].split(";")[0])
                assert [e.value for e in ENUMS[name]] == values, name
    assert seen == list(fields)
    assert INSTRUMENT_ID_RE.pattern == ticks(table(section(DATA_MD, "1. Instrument"))[0][3])[0]


def _expand(cell: str) -> list[str]:
    """``a1`` … ``a4`` → [a1, a2, a3, a4]; ``a``, ``b`` → [a, b]."""
    tokens = ticks(cell)
    if "…" in cell:
        first, last = tokens
        prefix = re.sub(r"\d+$", "", first)
        lo, hi = int(first[len(prefix) :]), int(last[len(prefix) :])
        return [f"{prefix}{n}" for n in range(lo, hi + 1)]
    return tokens


def _doc_catalogue() -> list[CatalogueEntry]:
    out = []
    for row in table(section(DATA_MD, "1.1")):
        ids = _expand(row[0])
        series: list[str | None] = [None] * len(ids)
        if row[1] != "generator":
            series = [*_expand(row[1])]
        assert len(series) == len(ids)

        def val(cell: str) -> str | None:
            return None if cell == "per name" else cell

        asset = val(row[2])
        for iid, ser in zip(ids, series, strict=True):
            out.append(
                CatalogueEntry(
                    iid,
                    ser,
                    AssetClass(asset) if asset else None,
                    InstrumentKind(row[3]),
                    val(row[4]),
                    val(row[5]),
                    val(row[6]),
                    Granularity(row[7]),
                )
            )
    return out


def test_catalogue_matches_doc() -> None:
    assert list(CATALOGUE_V1) == _doc_catalogue()
    assert len({e.instrument_id for e in CATALOGUE_V1}) == len(CATALOGUE_V1)
    for entry in CATALOGUE_V1:
        assert INSTRUMENT_ID_RE.fullmatch(entry.instrument_id)
        assert Source(entry.instrument_id.split(".")[0])


def test_queue_and_schema_names_match_messaging_doc() -> None:
    entities = {ticks(r[0])[0]: r[1] for r in table(section(MESSAGING_MD, "1."))}
    assert entities == {
        messages.QUEUE_BACKTEST_JOBS: "queue",
        messages.QUEUE_BACKTEST_RESULTS: "queue",
        messages.QUEUE_TIERING_JOBS: "queue",
        messages.TOPIC_INGEST_EVENTS: "topic",
    }
    headings = re.findall(r"^### `([^`]+)`", MESSAGING_MD, re.M)
    assert headings == list(messages.MESSAGE_MODELS)
    for name, model in messages.MESSAGE_MODELS.items():
        assert get_args(model.model_fields["schema_"].annotation) == (name,)
