"""Pinned Azure Retail Prices snapshot (SPEC-09 section 2.4, T-501).

Resolves the declarative :data:`METERS` list against the public, unauthenticated Azure Retail
Prices API (region ``westeurope``, EUR, ``priceType eq 'Consumption'``) and writes
``docs/finops/prices.yaml`` deterministically. Every cost number in the repo is
``unit price (from this file) x quantity``; prices are never hard-coded (CLAUDE.md 2.2, 2.5).

Early deletion is NOT a generic meter. Blob tiers carry a minimum retention: Cool 30 days,
Cold 90 days, Archive 180 days. Deleting or moving a blob earlier is billed as the remaining
days of the tier's stored-data price. The API has explicit early-delete meters for Cold and
Archive only (``blob_cold_early_delete_gb``, ``blob_archive_early_delete_gb``); for Cool use
``blob_cool_stored_gb_month`` x remaining days / 30. Hot has no minimum.

Known API limitation: ``unitPrice`` is rounded to 4 decimals, so per-second or per-action meters
(Container Apps vCPU/GiB-second, Logic Apps actions) come back as ``0.0`` or heavily rounded.
Such meters are flagged ``precision_limited: true`` in the snapshot; consumers must not treat
them as free. Container Apps monthly free grants (180k vCPU-s, 360k GiB-s, 2M requests) are not
exposed by the API either.

CLI: ``uv run python -m tidal.finops.fetch_prices --out docs/finops/prices.yaml``
"""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from tidal.common.errors import DependencyUnavailable, TidalError
from tidal.common.logging import get_logger

API_URL = "https://prices.azure.com/api/retail/prices"
REGION = "westeurope"
CURRENCY = "EUR"
DEFAULT_OUT = Path("docs/finops/prices.yaml")
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 1.0

_log = get_logger(__name__)


class PriceResolutionError(TidalError):
    """A meter matched zero or several prices; the snapshot must not be written."""

    slug = "price-resolution"
    title = "Price meter could not be resolved"


@dataclass(frozen=True, slots=True)
class MeterSpec:
    """Declarative filter for exactly one Retail Prices API row.

    ``tier_min`` selects the pricing tier (``tierMinimumUnits``); ``meter_id`` pins one meter
    when the API lists identical duplicates. ``allow_zero`` marks legitimately free meters;
    ``precision_limited`` marks meters whose true price is lost to the API's 4-decimal rounding.
    """

    key: str
    service_name: str
    product_name: str
    sku_name: str
    meter_name: str
    tier_min: float = 0.0
    meter_id: str | None = None
    allow_zero: bool = False
    precision_limited: bool = False
    note: str = ""


def _m(key: str, service: str, product: str, sku: str, meter: str, **kw: Any) -> MeterSpec:
    return MeterSpec(key, service, product, sku, meter, **kw)


def _blob(key: str, sku: str, meter: str, **kw: Any) -> MeterSpec:
    return _m(key, "Storage", "Blob Storage", sku, meter, **kw)


_SQL = "SQL Database General Purpose - Serverless - Compute Gen5"
_CA = "Azure Container Apps"

# Needed by docs/finops/cost-model.md rows, SPEC-02 benchmarks (C1/C3) and SPEC-09 C1-C10.
METERS: tuple[MeterSpec, ...] = (
    # --- Blob Storage LRS: data at rest (Hot is tiered; tier 0 = first 51,200 GB) ---
    _blob("blob_hot_stored_gb_month", "Hot LRS", "Hot LRS Data Stored"),
    _blob("blob_cool_stored_gb_month", "Cool LRS", "Cool LRS Data Stored"),
    _blob("blob_cold_stored_gb_month", "Cold LRS", "Cold LRS Data Stored"),
    _blob("blob_archive_stored_gb_month", "Archive LRS", "Archive LRS Data Stored"),
    # --- Blob operations per 10k ---
    _blob("blob_hot_write_10k", "Hot LRS", "Hot LRS Write Operations"),
    _blob("blob_hot_read_10k", "Hot LRS", "Hot Read Operations"),
    _blob("blob_hot_other_10k", "Hot LRS", "All Other Operations"),
    _blob("blob_cool_write_10k", "Cool LRS", "Cool LRS Write Operations"),
    _blob("blob_cool_read_10k", "Cool LRS", "Cool Read Operations"),
    _blob("blob_cold_write_10k", "Cold LRS", "Cold LRS Write Operations"),
    _blob("blob_cold_read_10k", "Cold LRS", "Cold LRS Read Operations"),
    _blob("blob_archive_write_10k", "Archive LRS", "Archive LRS Write Operations"),
    _blob("blob_archive_read_10k", "Archive LRS", "Archive Read Operations"),
    # --- Blob retrieval and per-GB write (hidden costs of Cool/Cold/Archive) ---
    _blob("blob_cool_retrieval_gb", "Cool LRS", "Cool Data Retrieval"),
    _blob("blob_cold_retrieval_gb", "Cold LRS", "Cold LRS Data Retrieval"),
    _blob("blob_archive_retrieval_gb", "Archive LRS", "Archive Data Retrieval"),
    _blob("blob_cool_write_gb", "Cool LRS", "Cool LRS Data Write", note="per GB written into Cool"),
    # Early-delete meters exist for Cold and Archive only (see module docstring).
    _blob("blob_cold_early_delete_gb", "Cold LRS", "Cold LRS Early Delete", note="90-day minimum"),
    _blob(
        "blob_archive_early_delete_gb",
        "Archive LRS",
        "Archive LRS Early Delete",
        note="180-day minimum",
    ),
    # --- Azure SQL Database GP serverless Gen5 ---
    # The API lists two identical rows for 1 vCore (meter 2f5f8d96 effective 2020-01-01 and
    # 76ba5cef effective 2024-02-01, same price). Pin the newer one; the unit is per vCore-hour
    # (divide by 3600 for vCore-second). The "1 vCore - Free" row is the dev free offer, not used.
    _m(
        "sql_gp_serverless_vcore_hour",
        "SQL Database",
        _SQL,
        "1 vCore",
        "vCore",
        meter_id="76ba5cef-d9c0-5dd5-b6a3-070a0fe7e6a8",
    ),
    _m(
        "sql_gp_storage_gb_month",
        "SQL Database",
        "SQL Database Single/Elastic Pool General Purpose - Storage",
        "General Purpose",
        "General Purpose Data Stored",
    ),
    # --- Service Bus Basic ---
    _m(
        "servicebus_basic_ops_1m",
        "Service Bus",
        "Service Bus",
        "Basic",
        "Basic Messaging Operations",
    ),
    # --- Container Apps Consumption (4-dp rounding makes per-second meters read 0) ---
    _m(
        "containerapps_vcpu_active_second",
        _CA,
        _CA,
        "Standard",
        "Standard vCPU Active Usage",
        allow_zero=True,
        precision_limited=True,
        note="API rounds per-second price to 0.0000; free grant 180k vCPU-s/month",
    ),
    _m(
        "containerapps_vcpu_idle_second",
        _CA,
        _CA,
        "Standard",
        "Standard vCPU Idle Usage",
        allow_zero=True,
        precision_limited=True,
        note="API rounds per-second price to 0.0000",
    ),
    _m(
        "containerapps_memory_active_gib_second",
        _CA,
        _CA,
        "Standard",
        "Standard Memory Active Usage",
        allow_zero=True,
        precision_limited=True,
        note="API rounds per-second price to 0.0000; free grant 360k GiB-s/month",
    ),
    _m(
        "containerapps_memory_idle_gib_second",
        _CA,
        _CA,
        "Standard",
        "Standard Memory Idle Usage",
        allow_zero=True,
        precision_limited=True,
        note="API rounds per-second price to 0.0000",
    ),
    _m(
        "containerapps_requests_1m",
        _CA,
        _CA,
        "Standard",
        "Standard Requests",
        note="first 2M requests/month free (grant not in API)",
    ),
    # --- App Service B1 Linux (the non-Linux "B1 App" row is Windows) ---
    _m(
        "appservice_b1_linux_hour",
        "Azure App Service",
        "Azure App Service Basic Plan - Linux",
        "B1",
        "B1",
    ),
    # --- Virtual machines (Linux; Windows rows carry "Windows" in productName) ---
    _m("vm_b1s_hour", "Virtual Machines", "Virtual Machines BS Series", "B1s", "B1s"),
    _m("vm_b2s_hour", "Virtual Machines", "Virtual Machines BS Series", "B2s", "B2s"),
    _m(
        "vm_d4as_v5_hour",
        "Virtual Machines",
        "Virtual Machines Dasv5 Series",
        "Standard_D4as_v5",
        "D4as v5",
        note="on-demand; the Cloud Services row of the same name is a different product",
    ),
    _m(
        "vm_d4as_v5_spot_hour",
        "Virtual Machines",
        "Virtual Machines Dasv5 Series",
        "Standard_D4as_v5 Spot",
        "D4as v5 Spot",
        note="list spot price; real spot price varies with capacity, evictions possible",
    ),
    # --- Managed disks: P4 30 GiB = default Ubuntu OS disk, P10 128 GiB = AKS default OS disk,
    #     E4 32 GiB = Standard SSD alternative for the legacy VM ---
    _m("disk_premium_p4_month", "Storage", "Premium SSD Managed Disks", "P4 LRS", "P4 LRS Disk"),
    _m("disk_premium_p10_month", "Storage", "Premium SSD Managed Disks", "P10 LRS", "P10 LRS Disk"),
    _m(
        "disk_standard_ssd_e4_month",
        "Storage",
        "Standard SSD Managed Disks",
        "E4 LRS",
        "E4 LRS Disk",
    ),
    # --- ACR Basic ---
    _m(
        "acr_basic_registry_day",
        "Container Registry",
        "Container Registry",
        "Basic",
        "Basic Registry Unit",
    ),
    _m(
        "acr_basic_storage_gb_month",
        "Container Registry",
        "Container Registry",
        "Basic",
        "Data Stored",
        note="Basic includes 10 GiB in the registry unit",
    ),
    # --- Key Vault Standard ---
    _m("keyvault_standard_ops_10k", "Key Vault", "Key Vault", "Standard", "Operations"),
    # --- Log Analytics PerGB2018 (API name: "Analytics Logs"); first 5 GB/month free ---
    _m(
        "loganalytics_ingestion_free_gb",
        "Log Analytics",
        "Log Analytics",
        "Analytics Logs",
        "Analytics Logs Data Ingestion",
        allow_zero=True,
        note="tier 0: first 5 GB/month per billing account",
    ),
    _m(
        "loganalytics_ingestion_gb",
        "Log Analytics",
        "Log Analytics",
        "Analytics Logs",
        "Analytics Logs Data Ingestion",
        tier_min=5.0,
        note="paid price above the 5 GB/month free tier",
    ),
    _m(
        "loganalytics_retention_gb_month",
        "Log Analytics",
        "Log Analytics",
        "Analytics Logs",
        "Analytics Logs Data Retention",
        note="beyond the 31 days included",
    ),
    # --- Logic Apps Consumption (4-dp rounding distorts per-action prices) ---
    _m(
        "logicapp_consumption_builtin_action",
        "Logic Apps",
        "Logic Apps",
        "Consumption",
        "Consumption Built-in Actions",
        allow_zero=True,
        precision_limited=True,
        note="API rounds to 0.0000; first 4,000 built-in actions/month free",
    ),
    _m(
        "logicapp_consumption_standard_connector_action",
        "Logic Apps",
        "Logic Apps",
        "Consumption",
        "Consumption Standard Connector Actions",
        precision_limited=True,
        note="API rounds to 4 decimals; true per-action price differs from the shown value",
    ),
    # --- Bandwidth egress, Microsoft global network (default routing); first 100 GB free ---
    _m(
        "egress_free_gb",
        "Bandwidth",
        "Rtn Preference: MGN",
        "Standard",
        "Standard Data Transfer Out",
        allow_zero=True,
        note="tier 0: first 100 GB/month",
    ),
    _m(
        "egress_first_tier_gb",
        "Bandwidth",
        "Rtn Preference: MGN",
        "Standard",
        "Standard Data Transfer Out",
        tier_min=100.0,
        note="100 GB to 10 TB/month tier",
    ),
)


class Price(BaseModel):
    """One resolved meter as stored in ``prices.yaml``."""

    model_config = ConfigDict(frozen=True)

    key: str
    unit: str
    unitPrice: float  # noqa: N815 - mirrors the Retail Prices API field name
    meterId: str  # noqa: N815
    productName: str  # noqa: N815
    skuName: str  # noqa: N815
    meterName: str  # noqa: N815
    effectiveStartDate: str  # noqa: N815
    tierMinimumUnits: float = 0.0  # noqa: N815
    precision_limited: bool = False
    note: str = ""


class PriceBook(BaseModel):
    """Typed view of ``prices.yaml`` for T-204 / T-503."""

    model_config = ConfigDict(frozen=True)

    captured_at: date
    source: str
    region: str
    currency: str
    prices: tuple[Price, ...] = Field(min_length=1)

    def get(self, key: str) -> Price:
        """Return the price for ``key`` or raise ``KeyError`` naming the missing key."""
        for price in self.prices:
            if price.key == key:
                return price
        msg = f"price key not in snapshot: {key}"
        raise KeyError(msg)

    def unit_price(self, key: str) -> float:
        """Return the EUR unit price for ``key``."""
        return self.get(key).unitPrice

    def keys(self) -> frozenset[str]:
        """All keys present in the snapshot."""
        return frozenset(p.key for p in self.prices)


def load_prices(path: Path | str = DEFAULT_OUT) -> PriceBook:
    """Load and validate a price snapshot written by :func:`write_prices`."""
    with Path(path).open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return PriceBook.model_validate(raw)


def _odata_quote(value: str) -> str:
    return value.replace("'", "''")


def build_filter(spec: MeterSpec) -> str:
    """OData ``$filter`` for a meter (tier and meter id are checked client-side)."""
    parts = [
        f"armRegionName eq '{REGION}'",
        "priceType eq 'Consumption'",
        f"serviceName eq '{_odata_quote(spec.service_name)}'",
        f"productName eq '{_odata_quote(spec.product_name)}'",
        f"skuName eq '{_odata_quote(spec.sku_name)}'",
        f"meterName eq '{_odata_quote(spec.meter_name)}'",
    ]
    return " and ".join(parts)


def _get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, str] | None,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """GET with exponential backoff on transport errors, 429 and 5xx."""
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = client.get(url, params=params)
        except httpx.TransportError as exc:
            last = type(exc).__name__
        else:
            code = resp.status_code
            if code == httpx.codes.TOO_MANY_REQUESTS or code >= httpx.codes.INTERNAL_SERVER_ERROR:
                last = f"HTTP {resp.status_code}"
            elif resp.status_code >= httpx.codes.BAD_REQUEST:
                raise DependencyUnavailable(f"Retail Prices API returned HTTP {resp.status_code}")
            else:
                data = resp.json()
                if not isinstance(data, dict):
                    raise DependencyUnavailable("Retail Prices API returned a non-object body")
                return data
        if attempt < MAX_ATTEMPTS - 1:
            _log.warning("retail_prices_retry", attempt=attempt + 1, reason=last)
            sleep(BACKOFF_BASE_S * 2**attempt)
    raise DependencyUnavailable(f"Retail Prices API unavailable after {MAX_ATTEMPTS} attempts")


def fetch_items(
    client: httpx.Client,
    odata_filter: str,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """All rows for ``odata_filter``, following ``NextPageLink`` until exhausted."""
    items: list[dict[str, Any]] = []
    url: str | None = API_URL
    params: dict[str, str] | None = {"currencyCode": f"'{CURRENCY}'", "$filter": odata_filter}
    while url:
        body = _get_json(client, url, params, sleep)
        items.extend(body.get("Items", []))
        url = body.get("NextPageLink")
        params = None  # NextPageLink already carries the query string
    return items


def resolve_meter(
    client: httpx.Client,
    spec: MeterSpec,
    sleep: Callable[[float], None] = time.sleep,
) -> Price:
    """Resolve ``spec`` to exactly one price, or raise :class:`PriceResolutionError`."""
    rows = [
        r
        for r in fetch_items(client, build_filter(spec), sleep)
        if r.get("currencyCode") == CURRENCY
        and r.get("armRegionName") == REGION
        and r.get("type") == "Consumption"
        and float(r.get("tierMinimumUnits", 0.0)) == spec.tier_min
        and (spec.meter_id is None or r.get("meterId") == spec.meter_id)
    ]
    if len(rows) != 1:
        found = sorted(f"{r.get('meterId')}@{r.get('effectiveStartDate')}" for r in rows)
        raise PriceResolutionError(
            f"meter '{spec.key}' matched {len(rows)} prices (expected 1): {found}"
        )
    row = rows[0]
    price = float(row["unitPrice"])
    if price == 0.0 and not spec.allow_zero:
        raise PriceResolutionError(f"meter '{spec.key}' resolved to a zero price unexpectedly")
    return Price(
        key=spec.key,
        unit=str(row["unitOfMeasure"]),
        unitPrice=price,
        meterId=str(row["meterId"]),
        productName=str(row["productName"]),
        skuName=str(row["skuName"]),
        meterName=str(row["meterName"]),
        effectiveStartDate=str(row["effectiveStartDate"]),
        tierMinimumUnits=spec.tier_min,
        precision_limited=spec.precision_limited,
        note=spec.note,
    )


def resolve_all(
    client: httpx.Client,
    specs: Sequence[MeterSpec] = METERS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Price]:
    """Resolve every spec, sorted by key; the first failure aborts (no partial snapshot)."""
    keys = [s.key for s in specs]
    if len(set(keys)) != len(keys):
        raise PriceResolutionError("duplicate meter keys in the meter spec")
    return sorted((resolve_meter(client, s, sleep) for s in specs), key=lambda p: p.key)


def render_yaml(prices: Sequence[Price], captured_at: date) -> str:
    """Deterministic YAML text: sorted keys, LF endings, entries sorted by key."""
    entries = []
    for p in sorted(prices, key=lambda x: x.key):
        entry: dict[str, Any] = p.model_dump()
        if not entry["precision_limited"]:
            del entry["precision_limited"]
        if not entry["note"]:
            del entry["note"]
        entries.append(entry)
    doc = {
        "captured_at": captured_at.isoformat(),
        "source": API_URL,
        "region": REGION,
        "currency": CURRENCY,
        "prices": entries,
    }
    header = (
        "# Pinned Azure Retail Prices snapshot (T-501). GENERATED - do not edit by hand.\n"
        "# Regenerate: uv run python -m tidal.finops.fetch_prices --out docs/finops/prices.yaml\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, width=100)


def write_prices(path: Path, prices: Sequence[Price], captured_at: date) -> None:
    """Write the snapshot with LF line endings regardless of platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_yaml(prices, captured_at))


def main(argv: Sequence[str] | None = None, client: httpx.Client | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tidal.finops.fetch_prices",
        description="Capture the pinned westeurope EUR Azure price snapshot.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output YAML path")
    args = parser.parse_args(argv)
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        prices = resolve_all(http)
    except TidalError as exc:
        print(f"fetch_prices failed: {exc.detail or exc.title}", file=sys.stderr)  # noqa: T201
        return 1
    finally:
        if own:
            http.close()
    write_prices(args.out, prices, datetime.now(UTC).date())
    print(f"wrote {len(prices)} priced meters to {args.out}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
