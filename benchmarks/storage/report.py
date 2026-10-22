"""Render a results file as Markdown + matplotlib charts, with MODELLED EUR columns (SPEC-02/09).

``uv run python -m benchmarks.storage.report --results DIR --prices docs/finops/prices.yaml \
  --out docs/finops/results/storage-benchmark.md --img docs/finops/results/img``

Wall/CPU/bytes are MEASURED (results file). Every EUR figure is MODELLED = quantity x unit price
from the pinned ``prices.yaml``; no price is hard-coded. Compute cost is deliberately excluded (C5):
Container Apps per-second meters are ``precision_limited`` (C5), so compute is reported MEASURED
from Cost Management in the Azure run, never modelled here.
"""

import argparse
import math
from collections import defaultdict
from pathlib import Path
from typing import Final

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from benchmarks.storage.results_schema import ResultRow, read_results
from tidal.finops.fetch_prices import PriceBook, load_prices

GIB: Final = 1024**3
READ_BLOCK: Final = 4 * 1024**2  # assumed bytes per blob read operation (range GET)
READ_KEY: Final = "blob_hot_read_10k"
EGRESS_KEY: Final = "egress_first_tier_gb"


def eur_per_month_at_rest(bytes_at_rest: int, prices: PriceBook, key: str) -> float:
    """MODELLED: ``bytes / 2^30 x unitPrice(key)`` (a ``1 GB/Month`` meter, billed per GiB)."""
    return bytes_at_rest / GIB * prices.unit_price(key)


def eur_per_1k_queries(
    bytes_read: int | None, prices: PriceBook, *, internet_egress: bool = False
) -> float | None:
    """MODELLED, transactions only by default: ``1000 x ceil(bytes_read / 4 MiB) / 10,000 x read``.

    The 4 MiB-per-GET operation count is a LOWER BOUND (real readers issue more, smaller range
    requests; T-203 counts real Azurite requests). Egress is 0 by default because SPEC-02 section
    5 queries from Container Apps in the same region; ``internet_egress=True`` adds
    ``bytes_read / 2^30 x egress_first_tier_gb`` (an upper bound for out-of-region readers).
    ``None`` when the format reports no bytes read.
    """
    if bytes_read is None:
        return None
    per_query = math.ceil(bytes_read / READ_BLOCK) / 10_000 * prices.unit_price(READ_KEY)
    if internet_egress:
        per_query += bytes_read / GIB * prices.unit_price(EGRESS_KEY)
    return 1000 * per_query


def _cell(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6f} (MODELLED)"


def render_markdown(
    rows: list[ResultRow],
    prices: PriceBook,
    env: dict[str, str],
    *,
    internet_egress: bool = False,
) -> str:
    """Results table (one line per format x workload x cache) plus env and EUR footnotes."""
    cols = [
        "Format", "Workload", "Cache", "p50 s", "p95 s", "rows/s", "CPU s (client)",
        "Bytes at rest", "Bytes read", "EUR/month at rest", "EUR/1k queries (lower bound)",
    ]  # fmt: skip
    if internet_egress:
        cols.append("EUR/1k queries incl. internet egress")
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols)
    lines = []
    for r in sorted(rows, key=lambda x: (x.format_id, x.workload_id, x.cache)):
        rate = "n/a" if r.rows_per_s is None else f"{r.rows_per_s:,.0f}"
        read = "n/a" if r.bytes_read is None else f"{r.bytes_read:,}"
        rest = eur_per_month_at_rest(r.bytes_at_rest, prices, r.storage_price_key)
        w1 = r.workload_id == "W1"
        q = None if w1 else eur_per_1k_queries(r.bytes_read, prices)
        cells = [
            r.format_id, r.workload_id, r.cache, f"{r.wall_p50_s:.4f}", f"{r.wall_p95_s:.4f}",
            rate, f"{r.cpu_s:.3f}", f"{r.bytes_at_rest:,}", read, _cell(rest), _cell(q),
        ]  # fmt: skip
        if internet_egress:
            q2 = None if w1 else eur_per_1k_queries(r.bytes_read, prices, internet_egress=True)
            cells.append(_cell(q2))
        lines.append("| " + " | ".join(cells) + " |")
    envs = ", ".join(f"{k}={v}" for k, v in sorted(env.items()))
    foot = (
        f"\nWall, CPU and byte columns are MEASURED; prices captured {prices.captured_at} "
        f"({prices.region}, {prices.currency}).\n\n"
        'Caveats: "cold" means a fresh connection only; the OS page cache is NOT dropped, so '
        "cold understates a truly cold read. CPU s is client-process CPU only (server-side SQL "
        "CPU is not included). W1 has no cache state (fresh store per run).\n\n"
        "[MODELLED] EUR/month at rest = bytes_at_rest / 2^30 x unitPrice(the format's "
        "`storage_price_key`, `blob_hot_stored_gb_month` for F5).\n\n"
        f"[MODELLED] EUR/1k queries = 1000 x ceil(bytes_read / 4 MiB) / 10,000 x `{READ_KEY}`. "
        "The 4 MiB-per-GET operation count is a lower bound (T-203 counts real Azurite requests). "
        "Egress is 0: queries run from Container Apps in the same region (SPEC-02 section 5)."
    )
    if internet_egress:
        foot += (
            f" The internet-egress column adds bytes_read / 2^30 x `{EGRESS_KEY}` (first paid "
            "tier, an upper bound)."
        )
    foot += (
        "\n\nCompute is excluded: Container Apps per-second meters are precision-limited, so "
        "compute is reported MEASURED from Cost Management (C5), never MODELLED here.\n\n"
        f"Environment: {envs}\n"
    )
    return f"{head}\n" + "\n".join(lines) + "\n" + foot


def render_charts(rows: list[ResultRow], img_dir: Path) -> list[Path]:
    """Bar charts: bytes at rest per format and warm p50 per workload per format."""
    img_dir.mkdir(parents=True, exist_ok=True)
    formats = sorted({r.format_id for r in rows})
    size = {r.format_id: r.bytes_at_rest / GIB for r in rows if r.workload_id != "W1"}
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(formats, [size.get(f, 0.0) for f in formats])
    ax.set_ylabel("GiB at rest (MEASURED)")
    ax.set_title("Storage size by format")
    p1 = img_dir / "size_by_format.png"
    fig.savefig(p1, dpi=120, bbox_inches="tight")
    plt.close(fig)

    p50: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if r.cache == "warm" or r.workload_id == "W1":
            p50[r.workload_id][r.format_id] = r.wall_p50_s
    fig, ax = plt.subplots(figsize=(8, 4))
    width = 0.8 / max(len(formats), 1)
    wids = sorted(p50)
    for i, f in enumerate(formats):
        ax.bar(
            [k + i * width for k in range(len(wids))],
            [p50[w].get(f, 0.0) for w in wids],
            width,
            label=f,
        )
    ax.set_xticks([k + 0.4 - width / 2 for k in range(len(wids))], wids)
    ax.set_yscale("log")
    ax.set_ylabel("p50 wall s (MEASURED, warm; W1 write)")
    ax.legend()
    p2 = img_dir / "p50_by_workload.png"
    fig.savefig(p2, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return [p1, p2]


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser(prog="benchmarks.storage.report")
    ap.add_argument("--results", type=Path, required=True, help="dir with results.parquet")
    ap.add_argument("--prices", type=Path, default=Path("docs/finops/prices.yaml"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--internet-egress", action="store_true", help="add the optional column")
    ap.add_argument("--img", type=Path, default=Path("docs/finops/results/img"))
    a = ap.parse_args(argv)
    rows, env = read_results(a.results / "results.parquet")
    prices = load_prices(a.prices)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(
        render_markdown(rows, prices, env, internet_egress=a.internet_egress), encoding="utf-8"
    )
    render_charts(rows, a.img)


if __name__ == "__main__":
    main()
