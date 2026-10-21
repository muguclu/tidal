"""Tests for the pinned price snapshot fetcher (T-501). No network: httpx.MockTransport only."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from tidal.common.errors import DependencyUnavailable
from tidal.finops import fetch_prices as fp

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMITTED = REPO_ROOT / "docs" / "finops" / "prices.yaml"

REQUIRED_KEYS = {
    *(f"blob_{t}_stored_gb_month" for t in ("hot", "cool", "cold", "archive")),
    *(f"blob_{t}_{op}_10k" for t in ("hot", "cool", "cold", "archive") for op in ("write", "read")),
    *(f"blob_{t}_retrieval_gb" for t in ("cool", "cold", "archive")),
    "sql_gp_serverless_vcore_hour",
    "sql_gp_storage_gb_month",
    "servicebus_basic_ops_1m",
    "containerapps_vcpu_active_second",
    "containerapps_memory_active_gib_second",
    "containerapps_requests_1m",
    "appservice_b1_linux_hour",
    "vm_b2s_hour",
    "vm_d4as_v5_hour",
    "vm_d4as_v5_spot_hour",
    "vm_b1s_hour",
    "disk_premium_p4_month",
    "acr_basic_registry_day",
    "keyvault_standard_ops_10k",
    "loganalytics_ingestion_gb",
    "loganalytics_retention_gb_month",
    "logicapp_consumption_standard_connector_action",
    "egress_first_tier_gb",
}


def row(spec: fp.MeterSpec, price: float = 0.5, **over: Any) -> dict[str, Any]:
    """A recorded-style API row matching ``spec``."""
    base: dict[str, Any] = {
        "currencyCode": "EUR",
        "tierMinimumUnits": spec.tier_min,
        "retailPrice": price,
        "unitPrice": price,
        "armRegionName": "westeurope",
        "location": "EU West",
        "effectiveStartDate": "2024-02-01T00:00:00Z",
        "meterId": spec.meter_id or f"id-{spec.key}",
        "meterName": spec.meter_name,
        "productName": spec.product_name,
        "skuName": spec.sku_name,
        "serviceName": spec.service_name,
        "unitOfMeasure": "1 GB/Month",
        "type": "Consumption",
    }
    base.update(over)
    return base


def spec_for(key: str) -> fp.MeterSpec:
    return next(s for s in fp.METERS if s.key == key)


def client_for(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def catalogue_handler() -> Any:
    """Answer each filter with the rows of every spec sharing it (tiers differ per spec)."""

    def handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params["$filter"]
        rows = [
            row(s, 0.0 if s.allow_zero else 1.25) for s in fp.METERS if fp.build_filter(s) == flt
        ]
        return httpx.Response(200, json={"Items": rows})

    return handler


def test_build_filter_contains_all_dimensions() -> None:
    flt = fp.build_filter(spec_for("blob_cool_retrieval_gb"))
    assert "armRegionName eq 'westeurope'" in flt
    assert "priceType eq 'Consumption'" in flt
    assert "serviceName eq 'Storage'" in flt
    assert "skuName eq 'Cool LRS'" in flt
    assert "meterName eq 'Cool Data Retrieval'" in flt


def test_odata_quote_escapes_apostrophes() -> None:
    spec = fp.MeterSpec("k", "S", "it's", "sku", "m")
    assert "productName eq 'it''s'" in fp.build_filter(spec)


def test_meter_keys_unique_and_cover_required() -> None:
    keys = [s.key for s in fp.METERS]
    assert len(keys) == len(set(keys))
    assert set(keys) >= REQUIRED_KEYS


def test_pagination_follows_next_page_link() -> None:
    spec = spec_for("vm_b2s_hour")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "page2" in str(request.url):
            return httpx.Response(200, json={"Items": [row(spec, 0.04)], "NextPageLink": None})
        nxt = f"{fp.API_URL}?page2=1"
        other = row(spec, 9.0, tierMinimumUnits=99.0)  # ignored: wrong tier
        return httpx.Response(200, json={"Items": [other], "NextPageLink": nxt})

    price = fp.resolve_meter(client_for(handler), spec)
    assert len(seen) == 2
    assert price.unitPrice == 0.04
    assert price.key == "vm_b2s_hour"
    assert "currencyCode=%27EUR%27" in seen[0]


def test_zero_matches_fails_loudly() -> None:
    spec = spec_for("vm_b2s_hour")
    client = client_for(lambda _r: httpx.Response(200, json={"Items": []}))
    with pytest.raises(fp.PriceResolutionError, match="matched 0 prices"):
        fp.resolve_meter(client, spec)


def test_ambiguous_match_fails_loudly() -> None:
    spec = spec_for("vm_b2s_hour")
    rows = [row(spec, meterId="a"), row(spec, meterId="b")]
    client = client_for(lambda _r: httpx.Response(200, json={"Items": rows}))
    with pytest.raises(fp.PriceResolutionError, match="matched 2 prices"):
        fp.resolve_meter(client, spec)


def test_meter_id_pin_resolves_duplicates() -> None:
    spec = spec_for("sql_gp_serverless_vcore_hour")
    rows = [row(spec, meterId="2f5f8d96-old"), row(spec, meterId=spec.meter_id)]
    client = client_for(lambda _r: httpx.Response(200, json={"Items": rows}))
    assert fp.resolve_meter(client, spec).meterId == spec.meter_id


def test_wrong_region_or_currency_rows_are_ignored() -> None:
    spec = spec_for("vm_b2s_hour")
    rows = [row(spec, armRegionName="northeurope"), row(spec, currencyCode="USD")]
    client = client_for(lambda _r: httpx.Response(200, json={"Items": rows}))
    with pytest.raises(fp.PriceResolutionError):
        fp.resolve_meter(client, spec)


def test_unexpected_zero_price_rejected_but_allowed_when_declared() -> None:
    spec = spec_for("vm_b2s_hour")
    client = client_for(lambda _r: httpx.Response(200, json={"Items": [row(spec, 0.0)]}))
    with pytest.raises(fp.PriceResolutionError, match="zero price"):
        fp.resolve_meter(client, spec)
    free = spec_for("containerapps_vcpu_active_second")
    client = client_for(lambda _r: httpx.Response(200, json={"Items": [row(free, 0.0)]}))
    price = fp.resolve_meter(client, free)
    assert price.unitPrice == 0.0
    assert price.precision_limited is True


def test_retry_with_backoff_then_success() -> None:
    spec = spec_for("vm_b2s_hour")
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        if calls["n"] == 2:
            raise httpx.ConnectError("boom")
        if calls["n"] == 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"Items": [row(spec)]})

    price = fp.resolve_meter(client_for(handler), spec, sleeps.append)
    assert price.key == spec.key
    assert sleeps == [1.0, 2.0, 4.0]


def test_retries_exhausted_raises_dependency_unavailable() -> None:
    sleeps: list[float] = []
    client = client_for(lambda _r: httpx.Response(500))
    with pytest.raises(DependencyUnavailable):
        fp.fetch_items(client, "x", sleeps.append)
    assert len(sleeps) == fp.MAX_ATTEMPTS - 1


def test_client_error_not_retried() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400)

    with pytest.raises(DependencyUnavailable):
        fp.fetch_items(client_for(handler), "x", lambda _s: None)
    assert calls["n"] == 1


def test_non_object_body_rejected() -> None:
    client = client_for(lambda _r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(DependencyUnavailable):
        fp.fetch_items(client, "x", lambda _s: None)


def test_duplicate_spec_keys_rejected() -> None:
    spec = spec_for("vm_b2s_hour")
    with pytest.raises(fp.PriceResolutionError, match="duplicate"):
        fp.resolve_all(client_for(catalogue_handler()), [spec, spec])


def test_resolve_all_sorted_and_output_deterministic(tmp_path: Path) -> None:
    specs = [spec_for("vm_b2s_hour"), spec_for("acr_basic_registry_day"), spec_for("vm_b1s_hour")]
    prices = fp.resolve_all(client_for(catalogue_handler()), specs)
    assert [p.key for p in prices] == sorted(p.key for p in prices)

    when = date(2026, 1, 2)
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    fp.write_prices(a, prices, when)
    fp.write_prices(b, list(reversed(prices)), when)
    assert a.read_bytes() == b.read_bytes()
    assert b"\r" not in a.read_bytes()
    assert a.read_text(encoding="utf-8").startswith("# Pinned")


def test_loader_round_trip(tmp_path: Path) -> None:
    specs = [spec_for("containerapps_vcpu_active_second"), spec_for("vm_b2s_hour")]
    prices = fp.resolve_all(client_for(catalogue_handler()), specs)
    path = tmp_path / "p.yaml"
    fp.write_prices(path, prices, date(2026, 1, 2))
    book = fp.load_prices(path)
    assert book.captured_at == date(2026, 1, 2)
    assert (book.region, book.currency, book.source) == ("westeurope", "EUR", fp.API_URL)
    assert list(book.prices) == prices
    assert book.keys() == {"containerapps_vcpu_active_second", "vm_b2s_hour"}
    assert book.unit_price("vm_b2s_hour") == 1.25
    assert book.get("containerapps_vcpu_active_second").precision_limited is True
    with pytest.raises(KeyError, match="nope"):
        book.get("nope")


def test_cli_writes_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out" / "prices.yaml"
    rc = fp.main(["--out", str(out)], client=client_for(catalogue_handler()))
    assert rc == 0
    assert "priced meters" in capsys.readouterr().out
    book = fp.load_prices(out)
    assert book.keys() == {s.key for s in fp.METERS}


def test_cli_failure_exit_code_and_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "prices.yaml"
    client = client_for(lambda _r: httpx.Response(200, json={"Items": []}))
    assert fp.main(["--out", str(out)], client=client) == 1
    assert "matched 0 prices" in capsys.readouterr().err
    assert not out.exists()


def test_committed_snapshot_is_complete() -> None:
    book = fp.load_prices(COMMITTED)
    assert book.region == "westeurope"
    assert book.currency == "EUR"
    assert book.source == fp.API_URL
    assert book.keys() >= REQUIRED_KEYS
    assert book.keys() == {s.key for s in fp.METERS}
    assert b"\r" not in COMMITTED.read_bytes()
    for price in book.prices:
        assert price.meterId
        assert price.effectiveStartDate
        spec = spec_for(price.key)
        if price.unitPrice == 0.0:
            assert spec.allow_zero
