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

Decision (C5): Container Apps compute is reported as MEASURED from the Cost Management export,
never MODELLED from these meters. To make that impossible to get wrong by accident,
:meth:`PriceBook.get` and :meth:`PriceBook.unit_price` raise :class:`PrecisionLimitedPrice` for
``precision_limited`` meters unless the caller passes ``allow_imprecise=True``.

Unit conversions live in each entry's ``note`` (vCore-hour to second: divide by 3600; month of
hours: x730; egress tier boundaries). Not every meter is in ``westeurope``: Load Balancer rule
prices are published under region ``Global`` (see ``MeterSpec.region``).

CLI: ``uv run python -m tidal.finops.fetch_prices --out docs/finops/prices.yaml``
"""

import argparse
import contextlib
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from tidal.common.errors import DependencyUnavailable, TidalError
from tidal.common.logging import correlation_scope, get_logger
from tidal.common.timeutil import SystemClock

API_URL = "https://prices.azure.com/api/retail/prices"
REGION = "westeurope"
CURRENCY = "EUR"
DEFAULT_OUT = Path("docs/finops/prices.yaml")
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 1.0
MAX_BACKOFF_S = 60.0
MAX_PAGES = 200
API_HOST = "prices.azure.com"
JITTER_SEED = 0

_log = get_logger(__name__)


class PriceResolutionError(TidalError):
    """A meter matched zero or several prices; the snapshot must not be written."""

    slug = "price-resolution"
    title = "Price meter could not be resolved"


class PrecisionLimitedPrice(TidalError):  # noqa: N818 - name fixed by the T-501 review
    """A price rounded by the API (4 decimals) was requested without ``allow_imprecise=True``."""

    slug = "precision-limited-price"
    title = "Price is precision-limited"


class UntrustedPagination(DependencyUnavailable):
    """``NextPageLink`` left ``prices.azure.com`` or the page limit was exceeded."""

    slug = "untrusted-pagination"
    title = "Untrusted or runaway pagination"


@dataclass(frozen=True, slots=True)
class MeterSpec:
    """Declarative filter for exactly one Retail Prices API row.

    ``tier_min`` selects the pricing tier (``tierMinimumUnits``); ``meter_id`` pins one meter
    when the API lists identical duplicates. ``allow_zero`` marks legitimately free meters;
    ``precision_limited`` marks meters whose true price is lost to the API's 4-decimal rounding.
    ``region`` overrides the API ``armRegionName`` for meters published as ``Global``.
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
    region: str = REGION


# Any: **kw is forwarded verbatim to the typed MeterSpec constructor, which validates the names.
def _m(key: str, service: str, product: str, sku: str, meter: str, **kw: Any) -> MeterSpec:
    return MeterSpec(key, service, product, sku, meter, **kw)


# Any: forwarded to _m / MeterSpec, as above.
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
    # Blob index tags are billed per tag stored, per month (T-207): one meter for every tier/SKU
    # (same meterId); 5 tags per market-data part. Pinned by meter_id: the API repeats the row.
    _blob(
        "blob_index_tags_10k_month",
        "Hot LRS",
        "Index Tags",
        meter_id="2fb5f78a-0428-4e17-8fee-5b176a3ef34c",
        note="per 10K index tags per month (not per blob); a blob with 5 tags counts as 5",
    ),
    # --- Defender for SQL (T-407, ADR-0017): per server per month, not reduced by auto-pause ---
    _m(
        "defender_sql_server_month",
        "Microsoft Defender for Cloud",
        "Microsoft Defender for SQL",
        "Standard",
        "Standard Node",
        note="per server per month; billed regardless of auto-pause",
    ),
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
        note="per vCore-hour; vCore-second = unitPrice / 3600; billed per second of use",
    ),
    # Provisioned GP Gen5, the C2/C6 baseline ("GP 2 vCore" = 2 x this price). The API lists
    # the same meterId twice (effective 2018 and 2025, same price); identical rows are collapsed.
    _m(
        "sql_gp_provisioned_vcore_hour",
        "SQL Database",
        "SQL Database Single/Elastic Pool General Purpose - Compute Gen5",
        "1 vCore",
        "vCore",
        meter_id="1cd4ee5b-2b55-4dd9-b3d9-ae1b22af74b0",
        note=(
            "per vCore-hour, 1 vCore: multiply by vCores x 730 h/month. The API publishes no "
            "separate SQL licence meter; this rate is far below the serverless rate, so it may "
            "exclude the licence (Hybrid Benefit style). Treat the C2/C6 baseline as a lower "
            "bound and cross-check against the Cost Management export"
        ),
    ),
    _m(
        "sql_gp_storage_gb_month",
        "SQL Database",
        "SQL Database Single/Elastic Pool General Purpose - Storage",
        "General Purpose",
        "General Purpose Data Stored",
        note="per GB-month",
    ),
    # --- Service Bus Basic ---
    _m(
        "servicebus_basic_ops_1m",
        "Service Bus",
        "Service Bus",
        "Basic",
        "Basic Messaging Operations",
    ),
    # Standard adds a fixed base charge per namespace (accrues while it exists) plus operations.
    # The API publishes the base unit twice under the same sku/meter name: hourly (meter 9d0c5c5c)
    # and monthly (meter d5034e45). Both are pinned by meter_id; use ONE per calculation.
    _m(
        "servicebus_standard_base_hour",
        "Service Bus",
        "Service Bus",
        "Standard",
        "Standard Base Unit",
        meter_id="9d0c5c5c-f0ff-48c2-8c68-9ee7ffd306af",
        note="per namespace-hour; month = x730; fixed cost even when idle",
    ),
    _m(
        "servicebus_standard_base_month",
        "Service Bus",
        "Service Bus",
        "Standard",
        "Standard Base Unit",
        meter_id="d5034e45-00c1-48c6-8c00-c866f788e033",
        note="per namespace-month list price; alternative to the hourly meter, do not add both",
    ),
    # Operations are tiered in millions: tier 0 = 0.0 (first 13M included in the base unit),
    # 13M = first paid tier, then 100M and 2,500M tiers (not snapshotted).
    _m(
        "servicebus_standard_ops_included_1m",
        "Service Bus",
        "Service Bus",
        "Standard",
        "Standard Messaging Operations",
        allow_zero=True,
        note="tier 0: first 13 million operations/month are included (price 0.0)",
    ),
    _m(
        "servicebus_standard_ops_1m",
        "Service Bus",
        "Service Bus",
        "Standard",
        "Standard Messaging Operations",
        tier_min=13.0,
        note="per 1M operations, first paid tier (13M to 100M/month); tier minimum is in millions",
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
        note=(
            "per vCPU-second; API rounds to 0.0000, C5 is MEASURED from the cost export; "
            "free grant 180k vCPU-s/month"
        ),
    ),
    _m(
        "containerapps_vcpu_idle_second",
        _CA,
        _CA,
        "Standard",
        "Standard vCPU Idle Usage",
        allow_zero=True,
        precision_limited=True,
        note="per second; API rounds to 0.0000, C5 is MEASURED from the cost export",
    ),
    _m(
        "containerapps_memory_active_gib_second",
        _CA,
        _CA,
        "Standard",
        "Standard Memory Active Usage",
        allow_zero=True,
        precision_limited=True,
        note=(
            "per GiB-second; API rounds to 0.0000, C5 is MEASURED from the cost export; "
            "free grant 360k GiB-s/month"
        ),
    ),
    _m(
        "containerapps_memory_idle_gib_second",
        _CA,
        _CA,
        "Standard",
        "Standard Memory Idle Usage",
        allow_zero=True,
        precision_limited=True,
        note="per second; API rounds to 0.0000, C5 is MEASURED from the cost export",
    ),
    _m(
        "containerapps_requests_1m",
        _CA,
        _CA,
        "Standard",
        "Standard Requests",
        note="per 1M requests; first 2M requests/month free (grant not in API)",
    ),
    # --- App Service Linux (the non-Linux "B1 App" row is Windows) ---
    _m(
        "appservice_b1_linux_hour",
        "Azure App Service",
        "Azure App Service Basic Plan - Linux",
        "B1",
        "B1",
        note="per plan-hour; month = x730",
    ),
    # C5 baseline "always-on B2" is interpreted as an always-on App Service Basic B2 Linux plan
    # (2 vCPU / 3.5 GiB), the smallest always-on plan that matches the 1 vCPU / 2 GiB Container
    # Apps query service with headroom. A VM B2s (vm_b2s_hour) is the alternative reading.
    _m(
        "appservice_b2_linux_hour",
        "Azure App Service",
        "Azure App Service Basic Plan - Linux",
        "B2",
        "B2",
        note="per plan-hour; month = x730; C5 always-on baseline (see spec comment)",
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
        note="per hour; on-demand; the Cloud Services row of the same name is a different product",
    ),
    _m(
        "vm_d4as_v5_spot_hour",
        "Virtual Machines",
        "Virtual Machines Dasv5 Series",
        "Standard_D4as_v5 Spot",
        "D4as v5 Spot",
        note="per hour; list spot price; real spot price varies with capacity, evictions possible",
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
    # --- AKS networking: Standard LB rules and Standard static public IP ---
    # The Standard LB rule price is published under region "Global" (applies in westeurope).
    # "Included" = first 5 rules; AKS uses the Standard LB with one rule per service port.
    _m(
        "lb_standard_rules_hour",
        "Load Balancer",
        "Load Balancer",
        "Standard",
        "Standard Included LB Rules and Outbound Rules",
        region="Global",
        meter_id="27827eb0-7f60-4928-940b-f5fe15e7a4cb",
        note="per hour while the LB exists (first 5 rules); month = x730; region Global",
    ),
    _m(
        "publicip_standard_static_hour",
        "Virtual Network",
        "IP Addresses",
        "Standard",
        "Standard IPv4 Static Public IP",
        note="per hour; month = x730",
    ),
    # --- Private networking (T-404): private endpoints, Link data, DNS, NAT ---
    # Private Link and NAT gateway meters are published under region "Global"; DNS zones under "".
    _m(
        "private_endpoint_hour",
        "Virtual Network",
        "Virtual Network Private Link",
        "Standard",
        "Standard Private Endpoint",
        region="Global",
        note="per endpoint-hour; month = x730; region Global",
    ),
    _m(
        "private_link_data_ingress_gb",
        "Virtual Network",
        "Virtual Network Private Link",
        "Standard",
        "Standard Data Processed - Ingress",
        region="Global",
        note="per GB, first 1,000,000 GB tier; region Global",
    ),
    _m(
        "private_link_data_egress_gb",
        "Virtual Network",
        "Virtual Network Private Link",
        "Standard",
        "Standard Data Processed - Egress",
        region="Global",
        note="per GB, first 1,000,000 GB tier; region Global",
    ),
    _m(
        "private_dns_zone_month",
        "Azure DNS",
        "Azure DNS",
        "Private",
        "Private Zone",
        region="",
        meter_id="0b608a26-f611-4232-8192-ce81b6b57194",
        note="per hosted zone per month, first 25 zones; region blank in the API",
    ),
    _m(
        "private_dns_queries_1m",
        "Azure DNS",
        "Azure DNS",
        "Private",
        "Private Queries",
        region="Zone 1",
        note="per million queries; region Zone 1 in the API",
    ),
    _m(
        "nat_gateway_hour",
        "NAT Gateway",
        "NAT Gateway",
        "Standard",
        "Standard Gateway",
        region="Global",
        note="per gateway-hour; month = x730; region Global",
    ),
    _m(
        "nat_gateway_data_gb",
        "NAT Gateway",
        "NAT Gateway",
        "Standard",
        "Standard Data Processed",
        region="Global",
        note="per GB processed; region Global",
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
    # ACR Tasks bill vCPU-seconds (same meter on Basic/Standard/Premium). Tier 0 is the free
    # first 6,000 s/month (100 min); the paid tier is 0.0001 EUR/s as rounded by the API, so the
    # true price is unknown (precision-limited). Consumers must pass allow_imprecise=True.
    _m(
        "acr_task_vcpu_free_second",
        "Container Registry",
        "Container Registry",
        "Basic",
        "Task vCPU Duration",
        allow_zero=True,
        precision_limited=True,
        note="per vCPU-second; tier 0 = first 6,000 s/month free",
    ),
    _m(
        "acr_task_vcpu_second",
        "Container Registry",
        "Container Registry",
        "Basic",
        "Task vCPU Duration",
        tier_min=6000.0,
        precision_limited=True,
        note="per vCPU-second above 6,000 s/month; API rounds to 4 dp, so cost is approximate",
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
        note="per GB; tier 0 = first 5 GB/month per billing account",
    ),
    _m(
        "loganalytics_ingestion_gb",
        "Log Analytics",
        "Log Analytics",
        "Analytics Logs",
        "Analytics Logs Data Ingestion",
        tier_min=5.0,
        note="per GB; paid price above the 5 GB/month free tier",
    ),
    _m(
        "loganalytics_retention_gb_month",
        "Log Analytics",
        "Log Analytics",
        "Analytics Logs",
        "Analytics Logs Data Retention",
        note="per GB-month beyond the 31 days included",
    ),
    # C8 "basic logs table": Basic Logs ingestion is priced separately from Analytics Logs.
    _m(
        "loganalytics_basic_ingestion_gb",
        "Azure Monitor",
        "Azure Monitor",
        "Basic Logs",
        "Basic Logs Data Ingestion",
        note="per GB ingested into Basic Logs tables; queries are billed extra, not snapshotted",
    ),
    # --- Logic Apps Consumption (4-dp rounding distorts per-action prices) ---
    _m(
        "logicapp_consumption_builtin_action_free_tier",
        "Logic Apps",
        "Logic Apps",
        "Consumption",
        "Consumption Built-in Actions",
        allow_zero=True,
        precision_limited=True,
        note=(
            "FREE TIER row (tier 0, first 4,000 built-in actions/month); API rounds the "
            "paid price to 0.0000, so this is not the price beyond the free tier"
        ),
    ),
    _m(
        "logicapp_consumption_standard_connector_action",
        "Logic Apps",
        "Logic Apps",
        "Consumption",
        "Consumption Standard Connector Actions",
        precision_limited=True,
        note="per action; API rounds to 4 decimals, true price differs from the shown value",
    ),
    # --- Bandwidth egress, Microsoft global network (default routing); first 100 GB free ---
    _m(
        "egress_free_gb",
        "Bandwidth",
        "Rtn Preference: MGN",
        "Standard",
        "Standard Data Transfer Out",
        allow_zero=True,
        note="per GB; tier 0 = first 100 GB/month free (boundary 0-100 GB)",
    ),
    _m(
        "egress_first_tier_gb",
        "Bandwidth",
        "Rtn Preference: MGN",
        "Standard",
        "Standard Data Transfer Out",
        tier_min=100.0,
        note="per GB from 100 GB up to 10,335 GB/month; next tier starts at 10,335 GB",
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

    def get(self, key: str, *, allow_imprecise: bool = False) -> Price:
        """Return the price for ``key``.

        Raises ``KeyError`` for an unknown key and :class:`PrecisionLimitedPrice` for a
        ``precision_limited`` meter unless ``allow_imprecise=True``.
        """
        for price in self.prices:
            if price.key == key:
                if price.precision_limited and not allow_imprecise:
                    raise PrecisionLimitedPrice(
                        f"price '{key}' is rounded by the API and unsafe for cost numbers; "
                        "use the Cost Management export (MEASURED) or pass allow_imprecise=True"
                    )
                return price
        msg = f"price key not in snapshot: {key}"
        raise KeyError(msg)

    def unit_price(self, key: str, *, allow_imprecise: bool = False) -> float:
        """Return the EUR unit price for ``key`` (same guard as :meth:`get`)."""
        return self.get(key, allow_imprecise=allow_imprecise).unitPrice

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
        f"armRegionName eq '{_odata_quote(spec.region)}'",
        "priceType eq 'Consumption'",
        f"serviceName eq '{_odata_quote(spec.service_name)}'",
        f"productName eq '{_odata_quote(spec.product_name)}'",
        f"skuName eq '{_odata_quote(spec.sku_name)}'",
        f"meterName eq '{_odata_quote(spec.meter_name)}'",
    ]
    return " and ".join(parts)


def _retry_delay(attempt: int, retry_after: str | None, rng: random.Random) -> float:
    """Exponential backoff plus seeded jitter; a numeric ``Retry-After`` is a lower bound."""
    delay = BACKOFF_BASE_S * 2**attempt + rng.uniform(0.0, BACKOFF_BASE_S)
    if retry_after is not None:
        with contextlib.suppress(ValueError):  # HTTP-date form: keep the computed backoff
            delay = max(delay, float(retry_after))
    return float(min(delay, MAX_BACKOFF_S))


# Any: the JSON body is untyped external data; its shape is validated by the callers below.
def _get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, str] | None,
    sleep: Callable[[float], None],
    rng: random.Random,
) -> dict[str, Any]:
    """GET with backoff/jitter on transport errors, 429 and 5xx; honours ``Retry-After``."""
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        retry_after: str | None = None
        try:
            resp = client.get(url, params=params)
        except httpx.TransportError as exc:
            last = type(exc).__name__
        else:
            code = resp.status_code
            if code == httpx.codes.TOO_MANY_REQUESTS or code >= httpx.codes.INTERNAL_SERVER_ERROR:
                last = f"HTTP {code}"
                retry_after = resp.headers.get("Retry-After")
            elif code >= httpx.codes.BAD_REQUEST:
                raise DependencyUnavailable(f"Retail Prices API returned HTTP {code}")
            else:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise DependencyUnavailable("Retail Prices API returned invalid JSON") from exc
                if not isinstance(data, dict):
                    raise DependencyUnavailable("Retail Prices API returned a non-object body")
                return data
        if attempt < MAX_ATTEMPTS - 1:
            _log.warning("retail_prices_retry", attempt=attempt + 1, reason=last)
            sleep(_retry_delay(attempt, retry_after, rng))
    raise DependencyUnavailable(f"Retail Prices API unavailable after {MAX_ATTEMPTS} attempts")


def _check_next_link(link: str) -> None:
    """Only follow https links on ``prices.azure.com`` (no SSRF / credential-leak redirects)."""
    parsed = urlparse(link)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise UntrustedPagination("NextPageLink points outside prices.azure.com")


# Any: rows are raw API JSON objects, narrowed field by field in resolve_meter.
def fetch_items(
    client: httpx.Client,
    odata_filter: str,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """All rows for ``odata_filter``, following ``NextPageLink`` (same host, <= MAX_PAGES)."""
    rng = rng or random.Random(JITTER_SEED)  # noqa: S311 - jitter, not security
    items: list[dict[str, Any]] = []
    url: str | None = API_URL
    params: dict[str, str] | None = {"currencyCode": f"'{CURRENCY}'", "$filter": odata_filter}
    pages = 0
    while url:
        pages += 1
        if pages > MAX_PAGES:
            raise UntrustedPagination(f"pagination exceeded {MAX_PAGES} pages")
        body = _get_json(client, url, params, sleep, rng)
        items.extend(body.get("Items", []))
        url = body.get("NextPageLink")
        if url:
            _check_next_link(url)
        params = None  # NextPageLink already carries the query string
    return items


# Any: raw API JSON rows, as above.
def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse rows that are the same meter and price (API repeats them per effective date)."""
    best: dict[tuple[str, float, float], dict[str, Any]] = {}
    for r in rows:
        k = (str(r.get("meterId")), float(r["tierMinimumUnits"]), float(r["unitPrice"]))
        if k not in best or str(r["effectiveStartDate"]) > str(best[k]["effectiveStartDate"]):
            best[k] = r
    return list(best.values())


def resolve_meter(
    client: httpx.Client,
    spec: MeterSpec,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Price:
    """Resolve ``spec`` to exactly one price, or raise :class:`PriceResolutionError`."""
    rows = _dedupe(
        [
            r
            for r in fetch_items(client, build_filter(spec), sleep, rng)
            if r.get("currencyCode") == CURRENCY
            and r.get("armRegionName") == spec.region
            and r.get("type") == "Consumption"
            and float(r.get("tierMinimumUnits", 0.0)) == spec.tier_min
            and (spec.meter_id is None or r.get("meterId") == spec.meter_id)
        ]
    )
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
    rng: random.Random | None = None,
) -> list[Price]:
    """Resolve every spec, sorted by key; the first failure aborts (no partial snapshot)."""
    keys = [s.key for s in specs]
    if len(set(keys)) != len(keys):
        raise PriceResolutionError("duplicate meter keys in the meter spec")
    rng = rng or random.Random(JITTER_SEED)  # noqa: S311 - jitter, not security
    return sorted((resolve_meter(client, s, sleep, rng) for s in specs), key=lambda p: p.key)


def render_yaml(prices: Sequence[Price], captured_at: date) -> str:
    """Deterministic YAML text: sorted keys, LF endings, entries sorted by key."""
    entries = []
    for p in sorted(prices, key=lambda x: x.key):
        entry: dict[str, Any] = p.model_dump()  # Any: heterogeneous YAML scalars
        if not entry["precision_limited"]:
            del entry["precision_limited"]
        if not entry["note"]:
            del entry["note"]
        entries.append(entry)
    doc: dict[str, Any] = {  # Any: YAML document mixing scalars and a list of mappings
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
    with correlation_scope():
        try:
            prices = resolve_all(http)
        except TidalError as exc:
            print(f"fetch_prices failed: {exc.detail or exc.title}", file=sys.stderr)  # noqa: T201
            return 1
        except httpx.HTTPError as exc:
            msg = f"fetch_prices failed: HTTP client error ({type(exc).__name__})"
            print(msg, file=sys.stderr)  # noqa: T201
            return 1
        finally:
            if own:
                http.close()
        write_prices(args.out, prices, SystemClock().now().date())
        print(f"wrote {len(prices)} priced meters to {args.out}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
