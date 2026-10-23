#!/usr/bin/env python3
# ruff: noqa: E501, PLR2004, PLC0415, TID251  # standalone VM script: no tidal package, late third-party imports
"""Legacy daily-bar CSV exporter (T-310).

Runs on the legacy VM at every boot (systemd oneshot, see tidal-exporter.service):

1. reads the recent ``bars_1d`` Parquet parts from the ``marketdata`` container (storage-layout section 2),
2. writes one CSV feed for a downstream risk system to the ``exports`` container,
3. deallocates the VM it runs on, through the Azure Resource Manager API with the VM's managed identity
   (IMDS token, no secret, no Azure CLI), so the B1s compute meter stops (SPEC-07 section 2).

Authentication is the user-assigned managed identity ``id-legacy-vm`` only. Configuration comes from the
environment (config-contract keys); ``TIDAL_ENV`` is required and the script stops without it (ADR-0012).
It depends on pyarrow, azure-identity and azure-storage-blob only, so the VM does not need the tidal package.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

LOG = logging.getLogger("tidal.legacy.exporter")

SOURCE_CONTAINER = "marketdata"
EXPORT_CONTAINER = "exports"
DAILY_PREFIX = "v1/bars_1d/"
NS_PER_DAY = 86_400_000_000_000
CSV_COLUMNS = ("date", "instrument_id", "open", "high", "low", "close", "volume")
PARQUET_COLUMNS = ["ts_ns", "instrument_id", "open", "high", "low", "close", "volume", "version"]
VALID_ENVS = ("dev", "prod")

IMDS = "http://169.254.169.254/metadata"
ARM = "https://management.azure.com"
COMPUTE_API = "2024-07-01"

# v1/bars_1d/instrument=EIA.BRENT.SPOT/year=2024/month=00/part-00000.parquet
_PART = re.compile(
    r"^v1/bars_1d/instrument=(?P<instrument>[^/]+)/year=(?P<year>\d{4})/month=00/part-\d{5}\.parquet$"
)


class ConfigError(ValueError):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Exporter settings (non-secret)."""

    env: str
    account_url: str
    client_id: str | None
    lookback_days: int
    deallocate: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Config:
        """Build the config; ``TIDAL_ENV`` must be set explicitly to dev or prod (ADR-0012)."""
        env = environ.get("TIDAL_ENV", "")
        if env not in VALID_ENVS:
            raise ConfigError(f"TIDAL_ENV must be set to one of {VALID_ENVS}, got {env!r}")
        url = environ.get("TIDAL_STORAGE__ACCOUNT_URL", "")
        if not url.startswith("https://"):
            raise ConfigError("TIDAL_STORAGE__ACCOUNT_URL must be an https URL")
        try:
            days = int(environ.get("EXPORT_LOOKBACK_DAYS", "7"))
        except ValueError as exc:
            raise ConfigError("EXPORT_LOOKBACK_DAYS must be an integer") from exc
        if not 1 <= days <= 366:
            raise ConfigError("EXPORT_LOOKBACK_DAYS must be between 1 and 366")
        return cls(
            env=env,
            account_url=url.rstrip("/"),
            client_id=environ.get("AZURE_CLIENT_ID") or None,
            lookback_days=days,
            deallocate=environ.get("EXPORT_DEALLOCATE", "true").lower() != "false",
        )


def window_start_ns(now: datetime, lookback_days: int) -> int:
    """UTC-midnight epoch ns of the first exported trade date (daily bars sit at UTC midnight, ADR-0009)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=lookback_days)
    return int(start.timestamp()) * 1_000_000_000


def select_parts(blob_names: Iterable[str], now: datetime, lookback_days: int) -> list[str]:
    """Daily-bar part names whose year partition can hold rows of the window, sorted."""
    first_year = (now.astimezone(UTC) - timedelta(days=lookback_days)).year
    last_year = now.astimezone(UTC).year
    out = []
    for name in blob_names:
        m = _PART.match(name)
        if m and first_year <= int(m["year"]) <= last_year:
            out.append(name)
    return sorted(out)


def latest_versions(rows: Iterable[Mapping[str, Any]], start_ns: int) -> list[dict[str, Any]]:
    """Rows at or after ``start_ns``, one per (instrument, ts_ns) with the highest ``version`` (restatements)."""
    best: dict[tuple[str, int], Mapping[str, Any]] = {}
    for r in rows:
        ts = int(r["ts_ns"])
        if ts < start_ns:
            continue
        key = (str(r["instrument_id"]), ts)
        if key not in best or int(r.get("version", 0)) >= int(best[key].get("version", 0)):
            best[key] = r
    return [dict(best[k]) for k in sorted(best)]


def _num(value: Any) -> str:
    """CSV float: empty for missing/NaN (data-contracts 3.1), shortest round-trip repr otherwise (negatives kept)."""
    if value is None:
        return ""
    f = float(value)
    return "" if math.isnan(f) else repr(f)


def to_csv(rows: Iterable[Mapping[str, Any]]) -> str:
    """Render the feed: header plus one line per bar, in the given order, LF line endings."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        day = (
            datetime.fromtimestamp(int(r["ts_ns"]) // NS_PER_DAY * 86_400, tz=UTC)
            .date()
            .isoformat()
        )
        writer.writerow(
            [
                day,
                r["instrument_id"],
                *(_num(r.get(c)) for c in ("open", "high", "low", "close", "volume")),
            ]
        )
    return buf.getvalue()


def export_blob_name(run_date: datetime) -> str:
    """Blob name inside the ``exports`` container (retention 14 d by lifecycle, storage-layout section 1)."""
    return f"risk-feed/daily_bars_{run_date.astimezone(UTC).date().isoformat()}.csv"


# --- Blob I/O ---------------------------------------------------------------------------------


def read_rows(container: Any, names: Iterable[str]) -> list[dict[str, Any]]:
    """Read the listed parts through a ContainerClient and return their rows."""
    import pyarrow.parquet as pq  # imported late so the pure helpers need no third-party package

    rows: list[dict[str, Any]] = []
    for name in names:
        data = container.download_blob(name).readall()
        rows.extend(pq.read_table(io.BytesIO(data), columns=PARQUET_COLUMNS).to_pylist())
    return rows


def run_export(config: Config, now: datetime, service: Any) -> tuple[str, int]:
    """Export the window and return (blob name, row count). ``service`` is a BlobServiceClient."""
    source = service.get_container_client(SOURCE_CONTAINER)
    listed = (b.name for b in source.list_blobs(name_starts_with=DAILY_PREFIX))
    names = select_parts(listed, now, config.lookback_days)
    rows = latest_versions(read_rows(source, names), window_start_ns(now, config.lookback_days))
    name = export_blob_name(now)
    service.get_container_client(EXPORT_CONTAINER).upload_blob(
        name, to_csv(rows).encode("utf-8"), overwrite=True
    )
    LOG.info("exported %s rows=%d parts=%d", name, len(rows), len(names))
    return name, len(rows)


# --- Self-deallocate through IMDS + ARM -------------------------------------------------------

Fetch = Callable[[urllib.request.Request], tuple[int, bytes]]


def _fetch(req: urllib.request.Request) -> tuple[int, bytes]:
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https/IMDS URLs
        return int(resp.status), bytes(resp.read())


def imds_token(client_id: str | None, fetch: Fetch = _fetch) -> str:
    """Resource Manager access token of the VM's managed identity."""
    query = {"api-version": "2018-02-01", "resource": ARM + "/"}
    if client_id:
        query["client_id"] = client_id
    url = f"{IMDS}/identity/oauth2/token?{urllib.parse.urlencode(query)}"
    _, body = fetch(urllib.request.Request(url, headers={"Metadata": "true"}))  # noqa: S310
    return str(json.loads(body)["access_token"])


def vm_resource_id(fetch: Fetch = _fetch) -> str:
    """ARM resource ID of the VM this script runs on, from instance metadata."""
    url = f"{IMDS}/instance/compute?api-version=2021-02-01&format=json"
    _, body = fetch(urllib.request.Request(url, headers={"Metadata": "true"}))  # noqa: S310
    c = json.loads(body)
    return (
        f"/subscriptions/{c['subscriptionId']}/resourceGroups/{c['resourceGroupName']}"
        f"/providers/Microsoft.Compute/virtualMachines/{c['name']}"
    )


def deallocate_self(client_id: str | None, fetch: Fetch = _fetch) -> None:
    """POST .../deallocate on this VM. Needs only ``Microsoft.Compute/virtualMachines/deallocate/action`` on it."""
    token = imds_token(client_id, fetch)
    url = f"{ARM}{vm_resource_id(fetch)}/deallocate?api-version={COMPUTE_API}"
    req = urllib.request.Request(  # noqa: S310
        url, data=b"", method="POST", headers={"Authorization": f"Bearer {token}"}
    )
    status, _ = fetch(req)
    if status not in (200, 202):
        raise RuntimeError(f"deallocate returned HTTP {status}")
    LOG.info("deallocate requested")


def main(environ: Mapping[str, str] | None = None) -> int:
    """Entry point: export, then always deallocate (a stuck VM costs more than a missed feed)."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config.from_env(os.environ if environ is None else environ)
    code = 0
    try:
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        credential = ManagedIdentityCredential(client_id=config.client_id)
        service = BlobServiceClient(config.account_url, credential=credential)
        run_export(config, datetime.now(UTC), service)
    except Exception:
        LOG.exception("export failed")
        code = 1
    if config.deallocate:
        try:
            deallocate_self(config.client_id)
        except Exception:
            LOG.exception("deallocate failed; the 20:00 auto-shutdown is the safety net")
            code = code or 2
    return code


if __name__ == "__main__":
    sys.exit(main())
