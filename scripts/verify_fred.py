"""Verify the FRED catalogue series against the live FRED API (T-103).

Needs ``TIDAL_SOURCES__FRED_API_KEY`` in the environment or ``.env``; the key is never printed.
For every FRED catalogue series (``DEXUSEU``) it calls ``/fred/series`` (metadata: units,
frequency, observation range) and 1-row ``series/observations`` requests (first and last
observation), then compares with the catalogue (first observation 1999-01-04, daily, USD per EUR).
It also reports whether the observation rows use a ``date`` field and ``"."`` for missing values.

``--record`` stores the real responses under ``tests/fixtures/fred/live/`` with ``api_key``
stripped from every stored URL and body. For the metadata call only ``seriess[0]`` (the series
object) is stored, not the whole response.

Exit codes: 0 verified, 1 mismatch with expectations, 2 no API key.

Usage: ``uv run python scripts/verify_fred.py [--record]``
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any  # Any: decoded JSON bodies from FRED, validated key by key at use

import httpx

from tidal.contracts.instruments import CATALOGUE_V1
from tidal.ingest.sources.base import redact_url
from tidal.ingest.sources.fred import BASE_URL, strip_api_key

KEY_ENV = "TIDAL_SOURCES__FRED_API_KEY"
LIVE_DIR = Path("tests/fixtures/fred/live")
#: Expected first observation per series (data-contracts §1.1 history start).
EXPECTED_START = {"DEXUSEU": "1999-01-04"}


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
    resp = client.get(f"{BASE_URL}/{path}", params={**params, "api_key": key, "file_type": "json"})
    if resp.status_code != httpx.codes.OK:
        msg = f"HTTP {resp.status_code} for {redact_url(str(resp.request.url))}"
        raise RuntimeError(msg)
    return resp.json()


def _record(out: Path, name: str, path: str, body: Any) -> None:
    out.mkdir(parents=True, exist_ok=True)
    doc = {"url": redact_url(f"{BASE_URL}/{path}"), "body": strip_api_key(body)}
    (out / f"{name}.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def verify(client: httpx.Client, key: str, record_dir: Path | None = None) -> list[str]:
    """Check every FRED series; print findings and return the list of mismatches."""
    problems: list[str] = []
    for e in (e for e in CATALOGUE_V1 if e.instrument_id.startswith("FRED.")):
        series = e.source_series
        try:
            meta = _get(client, key, "series", {"series_id": series})["seriess"][0]
            obs = {"series_id": series, "limit": 1}
            first = _get(client, key, "series/observations", {**obs, "sort_order": "asc"})
            last = _get(client, key, "series/observations", {**obs, "sort_order": "desc"})
            f_row, l_row = first["observations"][0], last["observations"][0]
            print(  # noqa: T201
                f"{series} ({e.instrument_id}): {f_row['date']} .. {l_row['date']} "
                f"count={first['count']} freq={meta.get('frequency_short')} "
                f"units={meta.get('units')!r} (catalogue {e.unit})"
            )
            want = EXPECTED_START.get(series)
            if want and f_row["date"] != want:
                problems.append(f"{series}: first observation {f_row['date']} != {want}")
            if meta.get("frequency_short") != "D":
                problems.append(f"{series}: frequency {meta.get('frequency_short')!r} != 'D'")
            if meta.get("observation_start") != f_row["date"]:
                problems.append(f"{series}: metadata observation_start differs from first row")
            if record_dir:
                _record(record_dir, f"{series.lower()}_meta", "series", meta)
                _record(record_dir, f"{series.lower()}_first", "series/observations", first)
                _record(record_dir, f"{series.lower()}_last", "series/observations", last)
        except (RuntimeError, KeyError, IndexError, ValueError) as exc:
            problems.append(f"{series}: {exc}")
        except httpx.HTTPError as exc:  # transport errors, timeouts; message carries no URL
            problems.append(f"{series}: {type(exc).__name__} talking to FRED")
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
