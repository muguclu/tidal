# ruff: noqa: T201, E501, PLR2004  # standalone report script: prints the delta tables
"""Deterministic cost model behind docs/finops/deltas/T-207.md (CLAUDE.md 2.2: no number comes from a model).

Run from the repository root (stdlib + pyyaml, no randomness, no network):

    python -I docs/finops/deltas/T-207_model.py [docs/finops/prices.yaml]

Every EUR figure in the delta is printed here. Unit prices are read from prices.yaml (T-501); nothing is typed in.

Assumptions (stated, not measured):
- Scales (config-contract): S 3 instruments x 5 y, M 10 x 10 y, L 10 x 20 y; bytes 10 B/row -> 0.5 / 3 / 15 GB (T-121).
- Parts (storage-layout section 2): monthly parts for ticks, bars_1s, bars_1m, bars_1h = instruments x years x 12;
  yearly parts for bars_1d = instruments x years. One part per partition (post-compaction). Bytes are the tick bytes
  of T-121; bar datasets add parts (and tags) but are assumed to add negligible bytes.
- Age convention: data years have ages 1..years (the newest complete year is one year old); band = retag.age_band:
  hot < 3 y, cool 3..10 y, cold > 10 y. Bytes and parts are spread evenly over the data years.
- Hot is the only tier a part sits in until it is tagged compacted=true; the one-off move is not amortised.
- Index tags are billed on every tagged part in every tier, hot included.
- Cold data write has no meter in prices.yaml and is left out (unpriced).
- Hot-tier read operations on untagged/hot parts (0.0038 EUR/10K) are ignored as negligible.
"""

import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/finops/prices.yaml")
with path.open(encoding="utf-8") as fh:
    P = {p["key"]: p["unitPrice"] for p in yaml.safe_load(fh)["prices"]}

HOT, COOL, COLD = (P[f"blob_{t}_stored_gb_month"] for t in ("hot", "cool", "cold"))
TAG = P["blob_index_tags_10k_month"]
COOL_W, COLD_W = P["blob_cool_write_10k"], P["blob_cold_write_10k"]
COOL_WGB = P["blob_cool_write_gb"]
COOL_RET, COLD_RET = P["blob_cool_retrieval_gb"], P["blob_cold_retrieval_gb"]
COOL_R, COLD_R = P["blob_cool_read_10k"], P["blob_cold_read_10k"]
COLD_ED = P["blob_cold_early_delete_gb"]

SCALES = {"S": (3, 5, 0.5), "M": (10, 10, 3.0), "L": (10, 20, 15.0)}  # instruments, years, GB


def age_band(age: int) -> str:
    """Mirror of tidal.storage.retag.age_band for an age in whole years."""
    return "hot" if age < 3 else ("cool" if age <= 10 else "cold")


def split(years: int) -> tuple[float, float, float]:
    """Hot/cool/cold share of the data years, ages 1..years."""
    b = [age_band(a) for a in range(1, years + 1)]
    return b.count("hot") / years, b.count("cool") / years, b.count("cold") / years


def model(inst: int, years: int, gb: float) -> dict[str, float]:
    """All monthly/yearly/one-off figures of one scale."""
    fh, fc, fd = split(years)
    ticks = inst * years * 12
    allp = 4 * ticks + inst * years
    m: dict[str, float] = {"fh": fh, "fc": fc, "fd": fd, "ticks": ticks, "allp": allp}
    m["allhot"] = gb * HOT
    m["tiered"] = gb * (fh * HOT + fc * COOL + fd * COLD)
    m["saving"] = m["allhot"] - m["tiered"]
    for lab, parts in (("all", allp), ("tk", ticks)):
        m[f"{lab}_coolp"], m[f"{lab}_coldp"] = parts * fc, parts * fd
        m[f"{lab}_ops"] = m[f"{lab}_coolp"] / 1e4 * COOL_W + m[f"{lab}_coldp"] / 1e4 * COLD_W
        m[f"{lab}_dw"] = gb * fc * COOL_WGB
        m[f"{lab}_oneoff"] = m[f"{lab}_ops"] + m[f"{lab}_dw"]
        # steady state: one year of data crosses each boundary that exists in this scale's age range
        m[f"{lab}_cross_yr"] = parts / years / 1e4 * (COOL_W + (COLD_W if fd > 0 else 0.0))
        m[f"{lab}_cret_yr"] = gb * (fc * COOL_RET + fd * COLD_RET)
        m[f"{lab}_crops_yr"] = (m[f"{lab}_coolp"] * COOL_R + m[f"{lab}_coldp"] * COLD_R) / 1e4
        m[f"{lab}_comp_yr"] = m[f"{lab}_cret_yr"] + m[f"{lab}_crops_yr"]
        for n in (5, 2):
            m[f"{lab}_tags{n}"] = parts * n
            m[f"{lab}_tagcost{n}"] = parts * n / 1e4 * TAG
            m[f"{lab}_net{n}"] = (
                m["saving"]
                - m[f"{lab}_tagcost{n}"]
                - (m[f"{lab}_comp_yr"] + m[f"{lab}_cross_yr"]) / 12
            )
    return m


def breakeven_mb(n: int, fc: float, fd: float) -> float:
    """Average part size (MB, 1e6 B) at which tags + yearly compaction read ops equal the tiering saving.

    Hot-band parts pay tags and save nothing, so the shares fc/fd enter both sides. Returns inf if nothing tiers.
    """
    save_gb = fc * (HOT - COOL - COOL_RET / 12) + fd * (HOT - COLD - COLD_RET / 12)
    cost_part = n / 1e4 * TAG + (fc * COOL_R + fd * COLD_R) / 1e4 / 12
    return cost_part / save_gb * 1000 if save_gb > 0 else float("inf")


R = {s: model(*v) for s, v in SCALES.items()}


def row(label: str, key: str, fmt: str) -> None:
    """Print one markdown table row of a per-scale result."""
    print(f"| {label} | " + " | ".join(format(R[s][key], fmt) for s in SCALES) + " |")


print(f"prices used (EUR): hot {HOT} cool {COOL} cold {COLD} GB-month; tag {TAG}/10K tags-month;")
print(
    f"  write/10K cool {COOL_W} cold {COLD_W}; read/10K cool {COOL_R} cold {COLD_R}; cool data write/GB {COOL_WGB};"
)
print(
    f"  retrieval/GB cool {COOL_RET} cold {COLD_RET}; cold early delete {COLD_ED} per GB-month (x3 = 90 d)"
)
print("\n## Byte split by age_band (ages 1..years)\n| | S | M | L |\n|---|---|---|---|")
print("| data years | " + " | ".join(str(SCALES[s][1]) for s in SCALES) + " |")
print(
    "| hot / cool / cold | "
    + " | ".join(f"{R[s]['fh']:.2f} / {R[s]['fc']:.2f} / {R[s]['fd']:.2f}" for s in SCALES)
    + " |"
)
print("\n## Parts and bytes\n| | S | M | L |\n|---|---|---|---|")
print("| GB | " + " | ".join(str(SCALES[s][2]) for s in SCALES) + " |")
row("parts, ticks only", "ticks", ".0f")
row("parts, all five datasets", "allp", ".0f")
print("\n## At rest (EUR/month)\n| | S | M | L |\n|---|---|---|---|")
row("all Hot", "allhot", ".4f")
row("tiered by age_band", "tiered", ".4f")
row("saving", "saving", ".4f")
for lab, title in (
    ("all", "All five datasets tagged, 5 tags (current) and 2 tags"),
    ("tk", "Ticks-only tagged (bars untagged)"),
):
    print(f"\n## {title}\n| | S | M | L |\n|---|---|---|---|")
    row("tagged parts", "allp" if lab == "all" else "ticks", ".0f")
    row("tags at 5/part", f"{lab}_tags5", ".0f")
    row("tag cost at 5/part EUR/month", f"{lab}_tagcost5", ".4f")
    row("tags at 2/part", f"{lab}_tags2", ".0f")
    row("tag cost at 2/part EUR/month", f"{lab}_tagcost2", ".4f")
    row("cool parts moved", f"{lab}_coolp", ".0f")
    row("cold parts moved", f"{lab}_coldp", ".0f")
    row("one-off tier-change ops EUR", f"{lab}_ops", ".4f")
    row("one-off Cool data write EUR", f"{lab}_dw", ".4f")
    row("one-off total EUR", f"{lab}_oneoff", ".4f")
    row("crossing ops EUR/year (steady state)", f"{lab}_cross_yr", ".4f")
    row("compaction retrieval EUR/year", f"{lab}_cret_yr", ".4f")
    row("compaction read ops EUR/year", f"{lab}_crops_yr", ".4f")
    row("compaction reads EUR/year total", f"{lab}_comp_yr", ".4f")
    row("net EUR/month at 5 tags", f"{lab}_net5", "+.4f")
    row("net EUR/month at 2 tags", f"{lab}_net2", "+.4f")

print("\n## Sensitivity: 45,000 parts at L (the earlier unsupported part count)")
for lab, (_h, fc, fd) in (
    ("T-121 20-year split 0.15/0.35/0.50", (0.15, 0.35, 0.5)),
    ("age_band split of L", split(20)),
):
    ops = 45000 * fc / 1e4 * COOL_W + 45000 * fd / 1e4 * COLD_W
    print(f"{lab}: one-off {ops + 15 * fc * COOL_WGB:.4f} EUR (ops {ops:.4f})")
print(
    f"\n## Early deletion maxima (EUR/GB): cold {3 * COLD_ED:.4f} (3 x per-GB-month meter), cool {COOL:.4f} (derived)"
)
print(
    "\n## Break-even average part size, MB (tags + yearly compaction read ops = saving; one-off not amortised)"
)
print("| tags per part | S | M | L |\n|---|---|---|---|")
for n in (5, 2):
    print(
        f"| {n} | "
        + " | ".join(format(breakeven_mb(n, R[s]["fc"], R[s]["fd"]), ".2f") for s in SCALES)
        + " |"
    )
print(
    "| avg ticks part (MB) | "
    + " | ".join(format(SCALES[s][2] * 1000 / R[s]["ticks"], ".2f") for s in SCALES)
    + " |"
)
print(
    "| avg part, all five datasets (MB) | "
    + " | ".join(format(SCALES[s][2] * 1000 / R[s]["allp"], ".2f") for s in SCALES)
    + " |"
)
