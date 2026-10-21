"""data-contracts.md / messaging-contract.md tables == code mirror (ADR-0001)."""

import re
from pathlib import Path
from typing import Any, get_args

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import BaseModel

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
from tidal.contracts.messages import BacktestJob, BacktestJobResult, JobError, TieringJob
from tidal.contracts.schemas import (
    INTERVAL_CODES,
    PARQUET_ENCODINGS,
    QUALITY_RESERVED_MASK,
    SCHEMAS,
    QualityFlag,
    pyarrow_write_kwargs,
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
        series = [row[1]] * len(ids) if row[1] == "generator" else _expand(row[1])
        assert len(series) == len(ids)
        for iid, ser in zip(ids, series, strict=True):
            out.append(
                CatalogueEntry(
                    iid,
                    ser,
                    AssetClass(row[2]),
                    InstrumentKind(row[3]),
                    row[4],
                    row[5],
                    row[6],
                    Granularity(row[7]),
                )
            )
    return out


def test_catalogue_matches_doc() -> None:
    assert list(CATALOGUE_V1) == _doc_catalogue()
    assert len(CATALOGUE_V1) == 22
    assert len({e.instrument_id for e in CATALOGUE_V1}) == len(CATALOGUE_V1)
    for entry in CATALOGUE_V1:
        inst = entry.to_instrument()  # validates id regex, enums, currency
        assert inst.source == entry.instrument_id.split(".")[0]
        assert inst.roll_method is None
        assert inst.description == entry.description


def test_catalogue_descriptions_follow_doc_rule() -> None:
    assert "`<source> <series or name>, <kind>, <unit>`" in section(DATA_MD, "1.1")
    by_id = {e.instrument_id: e.description for e in CATALOGUE_V1}
    assert by_id["EIA.BRENT.SPOT"] == "EIA RBRTE, spot, USD/bbl"
    assert by_id["EIA.WTI.FUT2"] == "EIA RCLC2, future, USD/bbl"
    assert by_id["FRED.EURUSD.FX"] == "FRED DEXUSEU, fx, USD/EUR"
    assert by_id["SYN.POWER_DE.TICK"] == "SYN POWER_DE, spot, EUR/MWh"
    assert by_id["SYN.EURUSD.TICK"] == "SYN EURUSD, fx, USD/EUR"


def test_bar_encodings_follow_doc_paragraph() -> None:
    paragraph = section(DATA_MD, "2.")
    assert "Bars (§3) use the" in paragraph
    assert "`trade_count` writer default" in paragraph
    rle, bss, delta = "RLE_DICTIONARY", "BYTE_STREAM_SPLIT", "DELTA_BINARY_PACKED"
    assert PARQUET_ENCODINGS["BAR_V1"] == {
        "ts_ns": delta, "instrument_id": rle, "interval": rle, "open": bss, "high": bss,
        "low": bss, "close": bss, "volume": bss, "quality": rle, "ingested_at_ns": delta,
        "version": rle,
    }  # fmt: skip


def test_message_size_limit_matches_doc() -> None:
    assert f"{messages.MAX_MESSAGE_BYTES:,} bytes" in MESSAGING_MD


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


def _sample(f: Any) -> Any:  # Any: pyarrow-stubs generics add nothing for a 3-row fixture
    t = f.type
    if pa.types.is_dictionary(t):
        return pa.array(["A", "A", "B"]).dictionary_encode().cast(t)
    if pa.types.is_string(t):
        return pa.array(["EIA.WTI.FUT1", "EIA.WTI.FUT1", "EIA.WTI.FUT2"], t)
    return pa.array([1, 2, 1], t)


@pytest.mark.parametrize("name", list(SCHEMAS))
def test_pyarrow_write_kwargs_produce_contract_encodings(name: str, tmp_path: Path) -> None:
    schema = SCHEMAS[name]
    table = pa.table([_sample(f) for f in schema], schema=schema)
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path, **pyarrow_write_kwargs(name))  # type: ignore[arg-type]
    assert pq.read_table(path).schema.equals(schema)
    meta = pq.ParquetFile(path).metadata.row_group(0)
    written = {meta.column(i).path_in_schema: meta.column(i).encodings for i in range(len(schema))}
    for column, encoding in PARQUET_ENCODINGS[name].items():
        assert encoding in written[column], (column, written[column])
        if encoding != "RLE_DICTIONARY":
            assert "RLE_DICTIONARY" not in written[column], column
    for column in set(schema.names) - set(PARQUET_ENCODINGS[name]):  # writer default, no dict
        assert "RLE_DICTIONARY" not in written[column], column


def _top_level_keys(block: str) -> set[str]:
    """Keys at brace depth 1 of a jsonc object (nested objects skipped)."""
    keys, depth = set(), 0
    for match in re.finditer(r'[{}]|"(\w+)"\s*:', block):
        if match.group(0) == "{":
            depth += 1
        elif match.group(0) == "}":
            depth -= 1
        elif depth == 1:
            keys.add(match.group(1))
    return keys


def _jsonc(heading: str) -> str:
    match = re.search(r"```jsonc\n(.*?)```", section(MESSAGING_MD, heading), re.S)
    assert match, heading
    return match.group(1)


@pytest.mark.parametrize(
    ("heading", "model"),
    [
        ("`backtest.job/v1`", BacktestJob),
        ("`backtest.result/v1`", BacktestJobResult),
        ("`tiering.job/v1`", TieringJob),
    ],
)
def test_message_payload_fields_match_doc(heading: str, model: type[BaseModel]) -> None:
    assert _top_level_keys(_jsonc(heading)) == set(model.model_fields)


def test_envelope_and_error_fields_match_doc() -> None:
    for envelope in messages.MESSAGE_MODELS.values():
        fields = {f.alias or name for name, f in envelope.model_fields.items()}
        assert _top_level_keys(_jsonc("2. Envelope")) == fields
    error = re.search(r'"error": (\{[^}]*\})', _jsonc("`backtest.result/v1`"))
    assert error
    assert _top_level_keys(error.group(1)) == set(JobError.model_fields)
