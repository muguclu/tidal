"""Tests for the pinned price snapshot fetcher (T-501). No network: httpx.MockTransport only."""

# Seeded random.Random is used for deterministic jitter, not security.
# ruff: noqa: S311

import random
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
    "sql_gp_provisioned_vcore_hour",
    "lb_standard_rules_hour",
    "publicip_standard_static_hour",
    "loganalytics_basic_ingestion_gb",
    "appservice_b2_linux_hour",
    "logicapp_consumption_builtin_action_free_tier",
    "servicebus_standard_base_hour",
    "servicebus_standard_base_month",
    "servicebus_standard_ops_included_1m",
    "servicebus_standard_ops_1m",
    "acr_task_vcpu_free_second",
    "acr_task_vcpu_second",
}


def row(spec: fp.MeterSpec, price: float = 0.5, **over: Any) -> dict[str, Any]:
    """A recorded-style API row matching ``spec``."""
    base: dict[str, Any] = {
        "currencyCode": "EUR",
        "tierMinimumUnits": spec.tier_min,
        "retailPrice": price,
        "unitPrice": price,
        "armRegionName": spec.region,
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
    # backoff 1, 2, 4 s plus seeded jitter in [0, 1) s
    assert len(sleeps) == 3
    for got, base in zip(sleeps, (1.0, 2.0, 4.0), strict=True):
        assert base <= got < base + fp.BACKOFF_BASE_S


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
    with pytest.raises(fp.PrecisionLimitedPrice):
        book.get("containerapps_vcpu_active_second")
    with pytest.raises(fp.PrecisionLimitedPrice):
        book.unit_price("containerapps_vcpu_active_second")
    imprecise = book.get("containerapps_vcpu_active_second", allow_imprecise=True)
    assert imprecise.precision_limited is True
    assert book.unit_price("containerapps_vcpu_active_second", allow_imprecise=True) == 0.0
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


def test_precision_limited_error_is_typed_and_mentions_measured() -> None:
    book = fp.load_prices(COMMITTED)
    with pytest.raises(fp.PrecisionLimitedPrice, match="MEASURED") as info:
        book.unit_price("containerapps_memory_idle_gib_second")
    assert info.value.slug == "precision-limited-price"


def test_every_precision_limited_meter_is_guarded_in_committed_snapshot() -> None:
    book = fp.load_prices(COMMITTED)
    limited = [p.key for p in book.prices if p.precision_limited]
    assert limited
    for key in limited:
        with pytest.raises(fp.PrecisionLimitedPrice):
            book.get(key)
    assert book.unit_price("vm_b2s_hour") > 0


def test_acr_task_meter_is_precision_limited_and_guarded() -> None:
    book = fp.load_prices(COMMITTED)
    for key in ("acr_task_vcpu_free_second", "acr_task_vcpu_second"):
        with pytest.raises(fp.PrecisionLimitedPrice):
            book.unit_price(key)
    assert book.get("acr_task_vcpu_second", allow_imprecise=True).unitPrice > 0
    assert book.get("acr_task_vcpu_free_second", allow_imprecise=True).unitPrice == 0.0


def test_servicebus_standard_meters_are_pinned_and_tiered() -> None:
    book = fp.load_prices(COMMITTED)
    assert book.unit_price("servicebus_standard_base_hour") > 0
    assert book.unit_price("servicebus_standard_base_month") > 0
    assert book.unit_price("servicebus_standard_ops_included_1m") == 0.0
    assert book.get("servicebus_standard_ops_1m").tierMinimumUnits == 13.0
    assert book.unit_price("servicebus_standard_ops_1m") > 0
    assert spec_for("servicebus_standard_base_hour").meter_id
    assert spec_for("servicebus_standard_base_month").meter_id


def test_free_tier_logic_app_key_is_named_and_noted() -> None:
    price = fp.load_prices(COMMITTED).get(
        "logicapp_consumption_builtin_action_free_tier", allow_imprecise=True
    )
    assert "FREE TIER" in price.note


def test_conversion_notes_present_in_committed_snapshot() -> None:
    book = fp.load_prices(COMMITTED)
    assert "/ 3600" in book.get("sql_gp_serverless_vcore_hour").note
    assert "10,335" in book.get("egress_first_tier_gb").note
    assert "vCPU-second" in book.get("containerapps_vcpu_active_second", allow_imprecise=True).note


def test_global_region_meter_resolves_and_filters_on_region() -> None:
    spec = spec_for("lb_standard_rules_hour")
    assert "armRegionName eq 'Global'" in fp.build_filter(spec)
    client = client_for(lambda _r: httpx.Response(200, json={"Items": [row(spec, 0.022)]}))
    assert fp.resolve_meter(client, spec).unitPrice == 0.022
    wrong = row(spec, 0.022, armRegionName="westeurope")
    client = client_for(lambda _r: httpx.Response(200, json={"Items": [wrong]}))
    with pytest.raises(fp.PriceResolutionError):
        fp.resolve_meter(client, spec)


def test_identical_duplicate_rows_collapse_but_price_conflicts_do_not() -> None:
    spec = spec_for("sql_gp_provisioned_vcore_hour")
    old = row(spec, 0.15, effectiveStartDate="2018-08-01T00:00:00Z")
    new = row(spec, 0.15, effectiveStartDate="2025-06-01T00:00:00Z")
    client = client_for(lambda _r: httpx.Response(200, json={"Items": [old, new]}))
    assert fp.resolve_meter(client, spec).effectiveStartDate == "2025-06-01T00:00:00Z"
    conflict = row(spec, 0.99)
    client = client_for(lambda _r: httpx.Response(200, json={"Items": [old, conflict]}))
    with pytest.raises(fp.PriceResolutionError, match="matched 2"):
        fp.resolve_meter(client, spec)


def test_retry_after_is_honoured_and_capped() -> None:
    rng = random.Random(1)
    assert fp._retry_delay(0, "30", rng) == 30.0
    assert fp._retry_delay(0, "9999", rng) == fp.MAX_BACKOFF_S
    assert fp._retry_delay(0, "Wed, 21 Oct 2026 07:28:00 GMT", rng) < 2.0  # HTTP-date ignored


def test_jitter_is_deterministic_with_seeded_rng() -> None:
    def run() -> list[float]:
        sleeps: list[float] = []
        client = client_for(lambda _r: httpx.Response(503))
        with pytest.raises(DependencyUnavailable):
            fp.fetch_items(client, "x", sleeps.append, random.Random(42))
        return sleeps

    assert run() == run()


def test_retry_after_header_used_in_sleep() -> None:
    spec = spec_for("vm_b2s_hour")
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "17"})
        return httpx.Response(200, json={"Items": [row(spec)]})

    fp.resolve_meter(client_for(handler), spec, sleeps.append)
    assert sleeps == [17.0]


def test_next_page_link_off_host_rejected() -> None:
    for link in (
        "https://evil.example/api",
        "http://prices.azure.com/api",
        "https://prices.azure.com.evil.io/x",
    ):
        body: dict[str, Any] = {"Items": [], "NextPageLink": link}
        client = client_for(lambda _r, b=body: httpx.Response(200, json=b))
        with pytest.raises(fp.UntrustedPagination):
            fp.fetch_items(client, "x", lambda _s: None)


def test_page_limit_enforced() -> None:
    body: dict[str, Any] = {"Items": [], "NextPageLink": f"{fp.API_URL}?skip=1"}
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=body)

    with pytest.raises(fp.UntrustedPagination, match="pages"):
        fp.fetch_items(client_for(handler), "x", lambda _s: None)
    assert calls["n"] == fp.MAX_PAGES


def test_invalid_json_body_is_typed_error() -> None:
    client = client_for(lambda _r: httpx.Response(200, content=b"<html>not json"))
    with pytest.raises(DependencyUnavailable, match="invalid JSON"):
        fp.fetch_items(client, "x", lambda _s: None)


def test_cli_invalid_json_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = client_for(lambda _r: httpx.Response(200, content=b"nope"))
    assert fp.main(["--out", str(tmp_path / "p.yaml")], client=client) == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_cli_http_error_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> list[fp.Price]:
        raise httpx.TooManyRedirects("loop")

    monkeypatch.setattr(fp, "resolve_all", boom)
    client = client_for(lambda _r: httpx.Response(200, json={"Items": []}))
    assert fp.main(["--out", str(tmp_path / "p.yaml")], client=client) == 1
    assert "HTTP client error" in capsys.readouterr().err
