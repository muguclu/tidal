"""Verify ``tidal.ingest.sources.eia.ROUTES`` against the live EIA v2 API (T-102).

Needs ``TIDAL_SOURCES__EIA_API_KEY`` in the environment or ``.env``; the key is never printed.
For every EIA catalogue series it calls ``/v2/{route}/`` (metadata) and 1-row ``data`` requests
(first and last period), then prints route, facet ids, frequencies and the period range. It also
looks at the ``process``/``product`` values of the first rows to show whether a ``series`` facet
alone is enough. Exit code 1 on any mismatch with ``ROUTES``.

``--record`` stores the real responses under ``tests/fixtures/eia/live/`` with ``api_key``
stripped from every stored URL.

Exit codes: 0 all routes verified, 1 mismatch with ``ROUTES``, 2 no API key.

Usage: ``uv run python scripts/verify_eia.py [--record]``
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any  # Any: decoded JSON bodies from EIA, validated key by key at use

import httpx

from tidal.contracts.instruments import CATALOGUE_V1
from tidal.ingest.sources.base import redact_url, strip_api_key
from tidal.ingest.sources.eia import BASE_URL, ROUTES

KEY_ENV = "TIDAL_SOURCES__EIA_API_KEY"
LIVE_DIR = Path("tests/fixtures/eia/live")


def load_key(env_file: Path = Path(".env")) -> str | None:
    """Key from the environment, else from ``.env``."""
    key = os.environ.get(KEY_ENV)
    if key or not env_file.is_file():
        return key
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == KEY_ENV:
            return value.strip().strip("\"'") or None
    return None


def _get(client: httpx.Client, key: str, path: str, params: dict[str, str | int]) -> Any:
    resp = client.get(f"{BASE_URL}/{path}", params={**params, "api_key": key})
    if resp.status_code != httpx.codes.OK:
        msg = f"HTTP {resp.status_code} for {redact_url(str(resp.request.url))}"
        raise RuntimeError(msg)
    return resp.json()


def _record(out: Path, name: str, url: str, body: Any) -> None:
    out.mkdir(parents=True, exist_ok=True)
    doc = {"url": redact_url(url), "body": strip_api_key(body, only="request")}
    (out / f"{name}.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _data_params(series: str, direction: str, length: int) -> dict[str, str | int]:
    return {
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": series,
        "sort[0][column]": "period",
        "sort[0][direction]": direction,
        "offset": 0,
        "length": length,
    }


def verify(client: httpx.Client, key: str, record_dir: Path | None = None) -> list[str]:
    """Check every EIA series; print findings and return the list of mismatches."""
    problems: list[str] = []
    meta: dict[str, Any] = {}
    for route in sorted(set(ROUTES.values())):
        try:
            meta[route] = body = _get(client, key, route + "/", {})["response"]
        except (RuntimeError, KeyError, ValueError) as exc:
            problems.append(f"{route}: metadata failed ({exc})")
            continue
        facets = [f["id"] for f in body.get("facets", [])]
        freqs = [f["id"] for f in body.get("frequency", [])]
        print(f"route {route}: facets={facets} frequency={freqs}")  # noqa: T201
        if "series" not in facets:
            problems.append(f"{route}: no 'series' facet (have {facets})")
        if "daily" not in freqs:
            problems.append(f"{route}: no 'daily' frequency (have {freqs})")
        if record_dir:
            _record(record_dir, route.replace("/", "_") + "_meta", f"{BASE_URL}/{route}/", body)
    for e in (e for e in CATALOGUE_V1 if e.instrument_id.startswith("EIA.")):
        series, route = e.source_series, ROUTES[e.source_series]
        if route not in meta:
            continue
        try:
            first = _get(client, key, f"{route}/data/", _data_params(series, "asc", 5))["response"]
            last = _get(client, key, f"{route}/data/", _data_params(series, "desc", 1))["response"]
            rows = first["data"]
            if not rows:
                problems.append(f"{series}: no rows on {route}")
                continue
            procs = sorted({str(r.get("process")) for r in rows})
            if len(procs) > 1:
                problems.append(f"{series}: several 'process' values {procs}; add a process facet")
            if {r.get("series") for r in rows} != {series}:
                problems.append(f"{series}: response series differ from request")
            print(  # noqa: T201
                f"{series} ({e.instrument_id}): {route} {rows[0]['period']} .. "
                f"{last['data'][0]['period']} total={first['total']} process={procs}"
            )
            if record_dir:
                _record(record_dir, f"{series.lower()}_first", f"{BASE_URL}/{route}/data/", first)
                _record(record_dir, f"{series.lower()}_last", f"{BASE_URL}/{route}/data/", last)
        except (RuntimeError, KeyError, IndexError, ValueError) as exc:
            problems.append(f"{series}: {exc}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("--record", action="store_true", help="save responses under the live fixtures")
    args = ap.parse_args(argv)
    key = load_key()
    if not key:
        print(f"{KEY_ENV} is not set (environment or .env)", file=sys.stderr)  # noqa: T201
        return 2
    with httpx.Client(timeout=30.0) as client:
        problems = verify(client, key, LIVE_DIR if args.record else None)
    for p in problems:
        print("MISMATCH:", p, file=sys.stderr)  # noqa: T201
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
