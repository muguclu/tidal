"""Pydantic API models == docs/contracts/openapi.yaml components (T-003 vs T-004 drift guard).

Compares, per component and recursively into object properties / array items: the property set,
the required set, nullability, enum values and ``additionalProperties: false``. Cosmetic JSON
Schema differences (``anyOf`` vs type arrays, titles, formats, descriptions) are ignored on purpose
(SPEC-04 §4).
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tidal.contracts import export

OPENAPI: dict[str, Any] = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "docs/contracts/openapi.yaml").read_text("utf-8")
)
YAML_DEFS: dict[str, Any] = OPENAPI["components"]["schemas"]
PYD_DEFS: dict[str, Any] = json.loads(export.render()["api.v1.json"])["$defs"]
Schema = dict[str, Any]


class Side:
    """One side of the comparison: a ``$defs``-like mapping and its ``$ref`` prefix."""

    def __init__(self, defs: dict[str, Any], prefix: str) -> None:
        self.defs, self.prefix = defs, prefix

    def resolve(self, schema: Schema) -> Schema:
        while "$ref" in schema:
            schema = self.defs[schema["$ref"].removeprefix(self.prefix)]
        return schema

    def nullable(self, schema: Schema) -> bool:
        schema = self.resolve(schema)
        types = schema.get("type")
        if types == "null" or (isinstance(types, list) and "null" in types):
            return True
        if None in schema.get("enum", []):
            return True
        branches = schema.get("anyOf", []) + schema.get("oneOf", [])
        return any(self.nullable(b) for b in branches)

    def strip_null(self, schema: Schema) -> Schema:
        """``schema`` without a ``null`` branch (unresolved, so ``$ref`` names stay visible)."""
        for key in ("anyOf", "oneOf"):
            branches: list[Schema] = [
                b for b in schema.get(key, []) if self.resolve(b).get("type") != "null"
            ]
            if key in schema and len(branches) == 1:
                return branches[0]
        return schema

    def object_ref(self, schema: Schema) -> str | None:
        """Name of the referenced component if it is an object component, else ``None``."""
        ref = self.strip_null(schema).get("$ref")
        if ref is None:
            return None
        target = self.defs[ref.removeprefix(self.prefix)]
        return ref.rsplit("/", 1)[1] if {"properties", "allOf", "oneOf"} & set(target) else None

    def core(self, schema: Schema) -> Schema:
        """Resolved schema with a ``null`` branch removed and ``allOf`` merged."""
        schema = self.resolve(self.strip_null(self.resolve(schema)))
        if "allOf" in schema:
            merged: Schema = {"properties": {}, "required": []}
            for raw in schema["allOf"]:
                part = self.core(raw)
                merged["properties"] |= part.get("properties", {})
                merged["required"] += part.get("required", [])
            return merged
        return schema

    def enum(self, schema: Schema) -> set[Any] | None:
        schema = self.core(schema)
        if "const" in schema:
            return {schema["const"]}
        if "enum" in schema:
            return {v for v in schema["enum"] if v is not None}
        return None


Y = Side(YAML_DEFS, "#/components/schemas/")
P = Side(PYD_DEFS, "#/$defs/")


def compare(ys: Schema, ps: Schema, path: str, *, top: bool = False) -> None:
    """Assert structural equality of two schemas; object components are compared by name only
    below the top level (each is checked once by its own parametrised test)."""
    if not top and Y.object_ref(ys):  # inline yaml objects are compared structurally
        assert Y.object_ref(ys) == P.object_ref(ps), f"{path}: $ref"
        return
    y, p = Y.core(ys), P.core(ps)
    if y.get("type") == "array" or p.get("type") == "array":
        assert y.get("type") == p.get("type") == "array", path
        compare(y["items"], p["items"], f"{path}[]")
        return
    if "properties" not in y:
        assert Y.enum(y) == P.enum(p), f"{path}: enum"
        return
    assert set(y["properties"]) == set(p.get("properties", {})), f"{path}: properties"
    assert set(y.get("required", [])) == set(p.get("required", [])), f"{path}: required"
    if y.get("additionalProperties") is False:
        assert p.get("additionalProperties") is False, f"{path}: additionalProperties"
    for name, y_prop in y["properties"].items():
        p_prop = p["properties"][name]
        assert Y.nullable(y_prop) == P.nullable(p_prop), f"{path}.{name}: nullable"
        compare(y_prop, p_prop, f"{path}.{name}")


OBJECT_COMPONENTS = sorted(
    name
    for name, schema in YAML_DEFS.items()
    if name in PYD_DEFS and ("properties" in schema or "allOf" in schema)
)
KNOWN_DRIFT = {
    "BacktestMetrics": "openapi.yaml metrics are not nullable; api-contract §3 note says they are",
}


def test_every_api_component_is_compared() -> None:
    required = {
        "Problem", "Instrument", "InstrumentList", "Bar", "BarsResponse", "Tick", "TicksResponse",
        "LatestBar", "LatestResponse", "SpreadPoint", "SpreadResponse", "BacktestCosts",
        "BacktestRequestBase", "MaCrossoverRequest", "MeanReversionRequest",
        "CalendarSpreadRequest", "BacktestAccepted", "BacktestStatus", "BacktestChildren",
        "BacktestMetrics", "BacktestResult", "BacktestList", "TieringRunAccepted", "StorageStats",
    }  # fmt: skip
    assert required <= set(OBJECT_COMPONENTS)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, marks=pytest.mark.xfail(reason=KNOWN_DRIFT[n], strict=True))
        if n in KNOWN_DRIFT
        else n
        for n in OBJECT_COMPONENTS
    ],
)
def test_component_matches_openapi(name: str) -> None:
    compare(YAML_DEFS[name], PYD_DEFS[name], name, top=True)


@pytest.mark.parametrize("name", ["BarInterval", "BacktestJobState"])
def test_enum_components(name: str) -> None:
    pyd_name = {"BarInterval": "Interval", "BacktestJobState": "JobStatus"}[name]
    assert set(YAML_DEFS[name]["enum"]) == set(PYD_DEFS[pyd_name]["enum"])


def test_backtest_request_union_matches_openapi() -> None:
    y, p = YAML_DEFS["BacktestRequest"], PYD_DEFS["BacktestRequest"]
    assert [r["$ref"].rsplit("/", 1)[1] for r in y["oneOf"]] == [
        r["$ref"].rsplit("/", 1)[1] for r in p["oneOf"]
    ]
    assert y["discriminator"]["propertyName"] == p["discriminator"]["propertyName"]
    y_map = {k: v.rsplit("/", 1)[1] for k, v in y["discriminator"]["mapping"].items()}
    assert y_map == {k: v.rsplit("/", 1)[1] for k, v in p["discriminator"]["mapping"].items()}


@pytest.mark.parametrize(
    "ref", ["InstrumentId", "JobId", "TimeParam"], ids=["InstrumentId", "JobId", "TimeParam"]
)
def test_string_patterns_match_openapi(ref: str) -> None:
    where = {
        "InstrumentId": PYD_DEFS["Instrument"]["properties"]["instrument_id"],
        "JobId": PYD_DEFS["BacktestAccepted"]["properties"]["job_id"],
        "TimeParam": PYD_DEFS["BacktestRequestBase"]["properties"]["start"],
    }[ref]
    for key in ("pattern", "maxLength"):
        assert YAML_DEFS[ref].get(key) == where.get(key), (ref, key)


def test_health_bodies_match_openapi() -> None:
    statuses: set[str] = set()
    for path in ("/v1/health/live", "/v1/health/ready"):
        for response in OPENAPI["paths"][path]["get"]["responses"].values():
            body = response.get("content", {}).get("application/json", {}).get("schema")
            if body is None:
                continue
            assert set(body["properties"]) == set(PYD_DEFS["HealthStatus"]["properties"])
            assert set(body["required"]) == set(PYD_DEFS["HealthStatus"]["required"])
            statuses |= set(body["properties"]["status"]["enum"])
    assert statuses == set(PYD_DEFS["HealthStatus"]["properties"]["status"]["enum"])


def test_compare_detects_drift() -> None:
    bar = json.loads(json.dumps(PYD_DEFS["Bar"]))
    bar["properties"]["volume"] = {"type": "number"}
    with pytest.raises(AssertionError, match="volume: nullable"):
        compare(YAML_DEFS["Bar"], bar, "Bar", top=True)
    bar["required"].remove("volume")
    with pytest.raises(AssertionError, match="required"):
        compare(YAML_DEFS["Bar"], bar, "Bar", top=True)
    del bar["properties"]["volume"]
    with pytest.raises(AssertionError, match="properties"):
        compare(YAML_DEFS["Bar"], bar, "Bar", top=True)
    status = json.loads(json.dumps(PYD_DEFS["BacktestStatus"]))
    status["properties"]["children"] = {"$ref": "#/$defs/BacktestResult"}
    with pytest.raises(AssertionError, match=r"\$ref"):
        compare(YAML_DEFS["BacktestStatus"], status, "BacktestStatus", top=True)
