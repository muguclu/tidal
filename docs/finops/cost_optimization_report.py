# ruff: noqa: T201, E501, RUF001  # standalone report generator; en dashes are intentional in output
"""Generate docs/finops/cost-optimization.md (T-507) and the re-summed totals in docs/finops/cost-model.md.

CLAUDE.md 2.2: no number in the output is typed by hand. Every figure is read from the committed model scripts
(`results/c1_c3_model.py`, `results/c4_c10_model.py`, `deltas/T-404_model.py`, `deltas/T-407_model.py`,
`deltas/T-506_model.py`) executed via runpy, from `prices.yaml`, or from the local load-test CSV. Constants in the
SCENARIO block below are stated assumptions (labelled in the output). No randomness, no network.

    python -I docs/finops/cost_optimization_report.py

Side effect: `c1_c3_model.py` rewrites `results/c1-c3.md` as it always does (a no-op when nothing changed).
"""

import contextlib
import csv
import io
import runpy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIN = ROOT / "docs/finops"
PRICES = FIN / "prices.yaml"


def run(rel: str) -> dict:
    """Execute a model script with the prices path as argv[1], silencing its stdout; return its globals."""
    saved = sys.argv
    sys.argv = [rel, str(PRICES)]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return runpy.run_path(str(FIN / rel), run_name="t507")
    finally:
        sys.argv = saved


doc = yaml.safe_load(PRICES.read_text(encoding="utf-8"))
P = {p["key"]: p["unitPrice"] for p in doc["prices"]}
CAPTURED = doc["captured_at"]
G13 = run("results/c1_c3_model.py")
G410 = run("results/c4_c10_model.py")
G404 = run("deltas/T-404_model.py")
G506 = run(
    "deltas/T-506_model.py"
)  # defines main() only; alerts are recomputed below from the same prices
DEFENDER = P["defender_sql_server_month"]
HOURS = 730

# ---------------------------------------------------------------- SCENARIO (assumptions, labelled in the output)
SYSTEM_POOL_LOW_DOC = (
    10.0  # cost-model.md "10-35" low end; no formula in the repo (UNSOURCED input, flagged)
)
SB_DOC = 0.001  # cost-model.md Service Bus row (T-124 delta), tiny
KV_OPS = (10_000, 100_000)  # T-121 delta: 10k-100k operations per month
ACR_DAYS = 30
ALERT_5MIN, ALERT_15MIN_N = 1, 3  # T-506: one 5 min rule, three 15 min rules

# ---------------------------------------------------------------- C-row figures
c1, c2, R = G13["c1"], G13["c2"], G13["R"]
b4, spot, p4, od = G410["base"], G410["spot"], G410["p4"], G410["od"]
SPOT_H = G410["SPOT_NODE_HOURS"]
spot_cost = {k: h * (spot + p4 / HOURS) for k, h in SPOT_H.items()}
b2, b1 = G410["b2"], G410["b1"]
c5_base = b2 * HOURS
prov, stor, sv = G410["prov"], G410["stor"], G410["sv"]
sql_act = {k: v * G410["SQL_ACTIVE_H"] * sv + stor for k, v in G410["SQL_VCORES_ACTIVE"].items()}
c7_base = G410["b1s"] * HOURS + G410["e4"]
c7_opt = G410["b1s"] * G410["run_h"] + G410["e4"]
la = G410["la"]
capped = G410["capped"]
log_base = {k: la(g * 30) for k, g in G410["LOG_BASELINE_GB_DAY"].items()}
log_opt = la(capped)
acr_sg = P["acr_basic_storage_gb_month"]
acr_m12 = (
    max(0, 12 * G410["ACR_MERGES_MONTH"] * G410["ACR_IMAGE_GB"] - G410["ACR_INCLUDED_GB"]) * acr_sg
)
acr_m6 = (
    max(0, 6 * G410["ACR_MERGES_MONTH"] * G410["ACR_IMAGE_GB"] - G410["ACR_INCLUDED_GB"]) * acr_sg
)
always, partial, parked = G410["always"], G410["partial"], G410["parked"]

with (FIN / "results/load/local-run_stats.csv").open(encoding="utf-8") as fh:
    agg = next(r for r in csv.DictReader(fh) if r["Name"] == "Aggregated")
LOCAL_P50, LOCAL_P95, LOCAL_N = agg["50%"], agg["95%"], agg["Request Count"]

# ---------------------------------------------------------------- cost-model re-sum (T-507, folds T-506)
alerts = (
    ALERT_5MIN * P["alerts_log_rule_5min_month"] + ALERT_15MIN_N * P["alerts_log_rule_15min_month"]
)
blob_m = R["M"]["allhot"] - R["M"]["tk_net2"]  # M-scale storage at rest after net tiering effect
kv = tuple(n / 1e4 * P["keyvault_standard_ops_10k"] for n in KV_OPS)
acr_month = P["acr_basic_registry_day"] * ACR_DAYS
disks_lb_ip = p4 + (P["lb_standard_rules_hour"] + P["publicip_standard_static_hour"]) * HOURS
b1_month = b1 * HOURS
vm_active, vm_parked = c7_opt, G410["e4"]
sql_low, sql_high, sql_parked = (
    sql_act["0.5 vCore"],
    sql_act["2 vCore"],
    stor * 0 + G410["SQL_STORAGE_GB"] * P["sql_gp_storage_gb_month"],
)
sys_high = G410["b2s"] * HOURS
# (component, active low, active high, parked, source)
ROWS = [
    (
        "AKS system pool B2s",
        SYSTEM_POOL_LOW_DOC,
        sys_high,
        0.0,
        "low: documented input (UNSOURCED); high: 730 h x vm_b2s_hour",
    ),
    (
        "AKS spot pool + OS disk",
        spot_cost["light"],
        spot_cost["heavy"],
        0.0,
        "c4_c10_model.py scenarios 40 / 150 node-hours",
    ),
    (
        "AKS OS disk + LB + public IP",
        disks_lb_ip,
        disks_lb_ip,
        disks_lb_ip,
        "p4 + 730 h x (LB + IP); parked value assumes `az aks stop` keeps them (UNVERIFIED)",
    ),
    ("Container Apps", 0.0, 0.0, 0.0, "inside free grants (T-309 delta)"),
    (
        "App Service B1",
        b1_month,
        b1_month,
        b1_month,
        "730 h x appservice_b1_linux_hour; stopped B1 still bills",
    ),
    (
        "Azure SQL, list price",
        sql_low,
        sql_high,
        sql_parked,
        "0.5 / 2 vCore x 40 h x serverless rate + 32 GB; parked = storage",
    ),
    ("Storage (M scale)", blob_m, blob_m, blob_m, "T-207 model: all-Hot minus net tiering effect"),
    ("Service Bus Basic", SB_DOC, SB_DOC, 0.0, "documented input (T-124 delta)"),
    ("ACR Basic", acr_month, acr_month, acr_month, f"{ACR_DAYS} d x acr_basic_registry_day"),
    ("Key Vault", kv[0], kv[1], kv[0], "10k-100k ops x keyvault_standard_ops_10k"),
    (
        "Log Analytics ceiling",
        0.0,
        la(capped),
        0.0,
        "0.2 GB/day cap, 5 GB free (c4_c10_model.py la())",
    ),
    (
        "Alerts (T-506)",
        alerts,
        alerts,
        alerts,
        "1 x 5 min + 3 x 15 min log rules; blob audit ingestion stays inside the cap",
    ),
    ("Legacy VM + disk", vm_active, vm_active, vm_parked, "c4_c10_model.py C7"),
    ("Logic App, budget, TF state", 0.0, 0.0, 0.0, "free tier / free (T-310, T-125, T-008)"),
]
tot_lo = sum(r[1] for r in ROWS)
tot_hi = sum(r[2] for r in ROWS)
tot_pk = sum(r[3] for r in ROWS)
sql_free_lo = tot_lo - sql_low
sql_free_hi = tot_hi - sql_high
sql_free_pk = tot_pk - sql_parked
pk_no_app = tot_pk - b1_month
pk_deleted_aks = tot_pk - disks_lb_ip + p4
PNET_PIP, PNET_NAT = G404["total_pip"], G404["total_nat"]

# ---------------------------------------------------------------- formatting helpers
e = lambda x: f"{x:,.2f}"  # noqa: E731
pct = lambda a, b: f"{(1 - b / a) * 100:.1f} %"  # noqa: E731

o: list[str] = []
w = o.append

w("# Cost-optimisation report (T-507)")
w("")
w(
    f"_Generated by `docs/finops/cost_optimization_report.py` from `docs/finops/prices.yaml` (Azure Retail Prices API, westeurope, EUR, captured {CAPTURED}) "
    "and the committed model scripts. Do not edit by hand; re-run the script. Labels: **MEASURED** = counted by a run (local stack only so far), "
    "**MODELLED** = unit price x measured or stated quantity, **NEEDS-CLOUD** = needs a deployed environment and a real bill. "
    "Nothing in this repository has been deployed to Azure yet, so no EUR figure below is an invoice._"
)
w("")
w("## Executive summary (for the trading manager)")
w("")
w(
    f"* **What it costs.** A month with about 40 hours of real use is modelled at EUR {e(tot_lo)} to {e(tot_hi)} with the database at list price "
    f"(EUR {e(sql_free_lo)} to {e(sql_free_hi)} on the free database offer). Parked outside those hours it is EUR {e(tot_pk)} "
    f"(EUR {e(sql_free_pk)} on the free offer). The parked floor is mostly the Kubernetes load balancer, address and disk (EUR {e(disks_lb_ip)}), the web plan (EUR {e(b1_month)}) and the container registry (EUR {e(acr_month)}). MODELLED."
)
w(
    f"* **Biggest levers are compute that is switched off, not clever storage.** Backtest nodes on cheap interruptible machines that exist only while a sweep runs: "
    f"EUR {e(b4)} to EUR {e(spot_cost['light'])}-{e(spot_cost['heavy'])} per month ({pct(b4, spot_cost['heavy'])}-{pct(b4, spot_cost['light'])} less). "
    f"A database that pauses when idle: EUR {e(prov)} to EUR {e(sql_act['0.5 vCore'])}-{e(sql_act['2 vCore'])}. Parking the environment: EUR {e(always)} to EUR {e(partial)}-{e(parked)}. All MODELLED."
)
w(
    f"* **Storing the data smaller is real but worth cents.** The epoch Parquet format is {c1['s_f0_gib'] / c1['s_f5_gib']:.1f}x smaller than the relational baseline "
    f"(MEASURED bytes), EUR {c1['s_f0_eur']:.4f} versus EUR {c1['s_f5_eur']:.4f} per month at the 50 M-tick scale (MODELLED). "
    f"Moving old rows to cheaper blob tiers nets EUR {R['M']['tk_net2']:.4f} per month at the 300 M-tick scale; it is a design-correctness point, not a budget lever."
)
w(
    f"* **Measured vs modelled.** MEASURED: file sizes, the local SQL-to-blob migration (SQL space {G13['tick_kb_before'] + G13['bar_kb_before']:,} KB to {G13['tick_kb_after'] + G13['bar_kb_after']:,} KB reserved), "
    f"and local request latency (median {LOCAL_P50} ms over {LOCAL_N} requests, a warm laptop reference). Everything in EUR is MODELLED from list prices and stated usage assumptions, and the savings depend on those assumptions "
    f"(for example the serverless database stops saving money above {G13['break_even_hours']:.0f} active hours a month)."
)
w(
    "* **What still needs a cloud run.** All of C4 to C10 and the Azure-side parts of C1 to C3: real node-hours per 100 backtests, cold-start and database-resume delay (the price of scale-to-zero), "
    "the real log volume, and the actual bill. The checklist is at the end of this report; one week of the dev environment is enough to turn most rows from MODELLED into MEASURED."
)
w("")

# ---------------------------------------------------------------- register
s_c2 = c2["S"]
l_c3 = R["L"]
c3_after = l_c3["allhot"] - l_c3["tk_net2"]
w("## Register C1 to C10")
w("")
w(
    "EUR/month unless stated. Saving % = 1 - after / before. Evidence paths are relative to `docs/finops/`."
)
w("")
w("| # | Baseline | Optimised | Metric | Before -> after | Saving | Label | Evidence |")
w("|---|---|---|---|---|---|---|---|")
w(
    f"| C1 | F0 SQL rowstore | F5 epoch Parquet | bytes at rest, S-3m slice ({G13['t3m']:,} ticks) | {c1['3m_f0_gib'] * 1024:.2f} -> {c1['3m_f5_gib'] * 1024:.2f} MiB | {pct(c1['3m_f0_gib'], c1['3m_f5_gib'])} | MEASURED | [c1-c3.md](results/c1-c3.md), `results/storage-benchmark-2026-10-22.parquet` |"
)
w(
    f"| C1 | F0 (extrapolated) | F5 measured | EUR/month at rest, full S ({G13['ts']:,} ticks) | {c1['s_f0_eur']:.4f} -> {c1['s_f5_eur']:.4f} | {pct(c1['s_f0_eur'], c1['s_f5_eur'])} | MODELLED (F5 bytes MEASURED) | [c1-c3.md](results/c1-c3.md) |"
)
w(
    f"| C2 | all history in SQL GP 2 vCore | 90 d hot in serverless SQL + rest on Blob (S) | EUR/month | {e(s_c2['base_total'])} -> {e(s_c2['opt_total'])} | {pct(s_c2['base_total'], s_c2['opt_total'])} | MODELLED, NEEDS-CLOUD | [c1-c3.md](results/c1-c3.md), `results/c2/` |"
)
w(
    f"| C2 | all rows in local SQL | after `tidal tiering run` | `md.tick_hot` reserved KB (local) | {G13['tick_kb_before']:,} -> {G13['tick_kb_after']:,} | {pct(G13['tick_kb_before'], G13['tick_kb_after'])} | MEASURED (local) | `results/c2/dry_run.txt`, `run2_raw.txt` |"
)
w(
    f"| C3 | all Hot (L) | Hot/Cool/Cold by `age_band`, ticks only, 2 tags | EUR/month incl. tags and compaction reads | {l_c3['allhot']:.4f} -> {c3_after:.4f} | {pct(l_c3['allhot'], c3_after)} | MODELLED, tier capacity NEEDS-CLOUD | [c1-c3.md](results/c1-c3.md), `deltas/T-207_model.py` |"
)
w(
    f"| C4 | 5 x D4as_v5 on demand 24/7 | KEDA ScaledJob 0..N on spot, light / heavy | EUR/month compute | {e(b4)} -> {e(spot_cost['light'])} / {e(spot_cost['heavy'])} | {pct(b4, spot_cost['light'])} / {pct(b4, spot_cost['heavy'])} | MODELLED, NEEDS-CLOUD | [c4-c10.md](results/c4-c10.md) |"
)
w(
    f"| C5 | query service on always-on B2 | Container Apps, min replicas 0 | EUR/month | {e(c5_base)} -> 0.00 (inside free grants) | 100.0 % | MODELLED, NEEDS-CLOUD (p95 cold start) | [c4-c10.md](results/c4-c10.md) |"
)
w(
    f"| C6 | provisioned GP 2 vCore | serverless 0.5-2 vCore, pause 60 min (40 active h) | EUR/month | {e(prov)} -> {e(sql_act['0.5 vCore'])} - {e(sql_act['2 vCore'])} | {pct(prov, sql_act['0.5 vCore'])} - {pct(prov, sql_act['2 vCore'])} | MODELLED, NEEDS-CLOUD (resume latency) | [c4-c10.md](results/c4-c10.md) |"
)
w(
    f"| C7 | B1s 24/7 | auto-shutdown + scheduled start | EUR/month | {e(c7_base)} -> {e(c7_opt)} | {pct(c7_base, c7_opt)} | MODELLED, NEEDS-CLOUD | [c4-c10.md](results/c4-c10.md) |"
)
lb = list(log_base.items())
w(
    f"| C8 | default diagnostics, uncapped (assumed {lb[0][0]} / {lb[1][0]}) | 0.2 GB/day cap + 20 % sampling | EUR/month | {e(lb[0][1])} / {e(lb[1][1])} -> {e(log_opt)} | {pct(lb[0][1], log_opt)} / {pct(lb[1][1], log_opt)} | MODELLED, NEEDS-CLOUD | [c4-c10.md](results/c4-c10.md) |"
)
w(
    f"| C9 | no purge (month 6 / 12) | weekly `acr purge` | EUR/month | {e(acr_m6)} / {e(acr_m12)} -> 0.00 | 100.0 % | MODELLED, NEEDS-CLOUD | [c4-c10.md](results/c4-c10.md) |"
)
w(
    f"| C10 | env always on | `make park` outside work hours / fully parked | EUR/month | {e(always)} -> {e(partial)} / {e(parked)} | {pct(always, partial)} / {pct(always, parked)} | MODELLED, NEEDS-CLOUD | [c4-c10.md](results/c4-c10.md), `runbooks/park.md` |"
)
w("")
w(
    "Charts from the T-204 benchmark: ![size and EUR at S](results/img/size_eur_S.png) ![p50 at S](results/img/p50_S.png)"
)
w("")

# ---------------------------------------------------------------- sections
w("## C1. Epoch time-series format")
w("")
w(
    f"Method: the T-204 benchmark stores the same synthetic ticks in seven formats; bytes at rest are counted, then priced at SQL GP storage ({P['sql_gp_storage_gb_month']} EUR/GiB-month) versus Blob Hot ({P['blob_hot_stored_gb_month']}). F5 is {c1['3m_f0_gib'] / c1['3m_f5_gib']:.1f}x smaller on the 3-month slice (MEASURED) and {c1['s_f0_gib']:.3f} -> {c1['s_f5_gib']:.3f} GiB at full S (F0 MODELLED by bytes/tick, F5 MEASURED)."
)
w("")
w(
    "Evidence: [c1-c3.md](results/c1-c3.md), [storage-benchmark-2026-10-22.md](results/storage-benchmark-2026-10-22.md). Caveat: F0 was only run on the 3-month slice; the full-S F0 figure assumes linear row width. Absolute amounts are cents at every scale."
)
w("")
w("## C2. SQL to object storage migration")
w("")
w(
    f"Method: MEASURED on the local stack, `tidal tiering run` moved {G13['tick_rows']:,} tick rows and {G13['bar_rows']:,} bar rows to Parquet; the tick table went from {G13['tick_kb_before']:,} to {G13['tick_kb_after']:,} KB reserved, {G13['SQL_BPT']:.2f} B/tick in SQL versus {G13['PQ_BPT']:.2f} B/tick as Parquet. The EUR figure is MODELLED: always-on provisioned 2 vCore versus serverless 1 vCore average for {G13['SRV_HOURS']:.0f} h, 90 days hot, and {G13['QUERIES_PER_MONTH']:,} cold queries a month. S: {e(s_c2['base_total'])} -> {e(s_c2['opt_total'])}; M: {e(c2['M']['base_total'])} -> {e(c2['M']['opt_total'])}; L: {e(c2['L']['base_total'])} -> {e(c2['L']['opt_total'])}."
)
w("")
w(
    f"Evidence: [c1-c3.md](results/c1-c3.md), `results/c2/`. Caveat: the saving is SQL compute, which depends on the duty cycle; it vanishes at {G13['break_even_hours']:.0f} active hours a month at 1 vCore. The provisioned meter may exclude licence, so the baseline is a lower bound."
)
w("")
w("## C3. Blob lifecycle tiering by data age")
w("")
w(
    f"Method: MODELLED by `deltas/T-207_model.py` (ADR-0015: ticks only, two index tags). Net versus all-Hot: S {R['S']['tk_net2']:+.4f}, M {R['M']['tk_net2']:+.4f}, L {R['L']['tk_net2']:+.4f} EUR/month, after index-tag cost and compaction reads. With the earlier five-tag design the same model gives S {R['S']['tk_net5']:+.4f}, M {R['M']['tk_net5']:+.4f}, L {R['L']['tk_net5']:+.4f}, which is the tag-cost finding in the trade-offs below."
)
w("")
w(
    "Evidence: [c1-c3.md](results/c1-c3.md), `deltas/T-207.md`. Caveat: tier capacity (`BlobCapacity` by `Tier`) and the lifecycle engine need a real account; Azurite has neither."
)
w("")
w("## C4. Dynamic autoscaling (AKS spot, KEDA 0..N)")
w("")
w(
    f"Method: MODELLED, baseline 5 x D4as_v5 on demand x 730 h = {e(b4)}; optimised {SPOT_H['light']} / {SPOT_H['heavy']} spot node-hours (assumed) at {spot} EUR/h plus a prorated 32 GB P4 disk = {e(spot_cost['light'])} / {e(spot_cost['heavy'])}; idle cost of the pool is 0 at zero nodes. Spot/on-demand price ratio {spot / od:.3f}."
)
w("")
w(
    "Evidence: [c4-c10.md](results/c4-c10.md) (method for the cloud measurement of node-hours by pool and EUR per 100 backtests). Caveat: the node-hours are assumptions; EUR per 100 backtests is not modelled at all."
)
w("")
w("## C5. Serverless containers")
w("")
w(
    f"Method: MODELLED, B2 always on {HOURS} h x {b2} = {e(c5_base)} versus Container Apps with 90,000 of 180,000 free vCPU-seconds and 180,000 of 360,000 free GiB-seconds used. Compute meters round to 0.0 in the retail API, so the true saving is read from the cost export. Local warm reference (MEASURED, laptop): median {LOCAL_P50} ms, p95 {LOCAL_P95} ms over {LOCAL_N} requests; that p95 is warm-up noise, not a cold start."
)
w("")
w(
    "Evidence: [c4-c10.md](results/c4-c10.md). Caveat: cold-start p95 is the cost of this saving and is NEEDS-CLOUD."
)
w("")
w("## C6. SQL serverless auto-pause")
w("")
w(
    f"Method: MODELLED, provisioned 2 vCore = {e(prov)} (lower bound) versus serverless {G410['SQL_ACTIVE_H']} active hours at 0.5 vCore {e(sql_act['0.5 vCore'])} or 2 vCore {e(sql_act['2 vCore'])}; paused all month = {e(stor)} (storage). Dev uses the free offer (ADR-0011), about 0, until its allowance is used."
)
w("")
w(
    "Evidence: [c4-c10.md](results/c4-c10.md). Caveat: resume latency and the 60-minute idle tail billed at the active rate are NEEDS-CLOUD."
)
w("")
w("## C7. Scheduled VM")
w("")
w(
    f"Method: MODELLED, {HOURS} h x {P['vm_b1s_hour']} + {P['disk_standard_ssd_e4_month']} disk = {e(c7_base)} versus {G410['run_h']:.1f} h/month + disk = {e(c7_opt)}. The disk bills while deallocated, so the floor is {e(vm_parked)}."
)
w("")
w(
    "Evidence: [c4-c10.md](results/c4-c10.md). Caveat: running hours come from the activity log in the cloud window."
)
w("")
w("## C8. Log ingestion control")
w("")
w(
    f"Method: MODELLED, ceiling {G410['LOG_CAP_GB_DAY']} GB/day x 30 = {capped:.1f} GB/month, first {G410['LOG_FREE_GB']} GB free, rest {P['loganalytics_ingestion_gb']} EUR/GB = {e(log_opt)}. The uncapped baselines are assumed rates, not observations. A Basic Logs table would cost {e(capped * P['loganalytics_basic_ingestion_gb'])} for the same volume (no free tier), so it is not used for the capped path. The T-506 alert rules add {e(alerts)} EUR/month inside this budget."
)
w("")
w(
    "Evidence: [c4-c10.md](results/c4-c10.md), `deltas/T-120.md`, `deltas/T-506.md`. Caveat: when the cap is hit ingestion stops for the day."
)
w("")
w("## C9. ACR hygiene")
w("")
w(
    f"Method: MODELLED, assumed {G410['ACR_MERGES_MONTH']} images/month x {G410['ACR_IMAGE_GB']} GB, {G410['ACR_INCLUDED_GB']} GB included, {acr_sg} EUR/GB-month above. Without purge the bill grows to {e(acr_m6)} (month 6) and {e(acr_m12)} (month 12); with weekly purge it stays at the included amount."
)
w("")
w(
    "Evidence: [c4-c10.md](results/c4-c10.md), `deltas/T-122.md`. Caveat: small in EUR; the point is unbounded growth. Image size and cadence are assumptions."
)
w("")
w("## C10. Parking")
w("")
w(
    f"Method: MODELLED from the stoppable lines (AKS system B2s, App Service B1) and the retained ones (disk, LB, IP): always on {e(always)}; parked outside work hours ({G410['whrs']:.0f} h of {HOURS}) {e(partial)}; fully parked {e(parked)}. If Azure releases the LB and IP when the cluster stops the parked floor falls to the disk alone ({e(p4)}); this is UNVERIFIED (T-306, T-313)."
)
w("")
w(
    "Evidence: [c4-c10.md](results/c4-c10.md), `runbooks/park.md`. Caveat: the export is daily, so work-hour parking within a day is not visible and stays MODELLED."
)
w("")

# ---------------------------------------------------------------- trade-offs
w("## Trade-offs (SPEC-09 section 4), next to the saving")
w("")
w(
    f"* **C5 cold start.** Scale-to-zero saves {e(c5_base)}/month and costs the first request after idle: replica start, image pull and DuckDB Azure extension load. Cloud p95 is NEEDS-CLOUD. Lever: `min_replicas = 1`. Not for production if a trader waits on the first screen of the day."
)
w(
    f"* **C6 resume latency.** Auto-pause saves up to {e(prov - stor)}/month; the first connection after a pause waits tens of seconds (SPEC-09 says about a minute) or fails, and up to 60 min after the last connection bills at the active rate. Production would keep a floor of provisioned or longer pause delay."
)
w(
    f"* **C4 spot evictions.** Spot is {spot / od:.1%} of the on-demand price. A node can be evicted at any time; the job is redelivered from Service Bus and rerun, so a backtest can take longer and a sweep has a tail. Safe because backtests are deterministic and idempotent. Not for latency-bound work."
)
w(
    f"* **C3 tiering retrieval and early delete.** Cool and Cold charge per GB retrieved ({P['blob_cool_retrieval_gb']} / {P['blob_cold_retrieval_gb']} EUR/GB) and per 10 K operations; compaction of a tiered partition pays them again; Cold has a 90-day minimum ({P['blob_cold_early_delete_gb']} EUR/GB early-delete). Archive is excluded for market data (rehydration time, ADR-0004). The net gain is EUR {R['L']['tk_net2']:.4f}/month even at L."
)
w(
    f"* **Tag-cost finding (ADR-0015).** Index tags bill per tag per month in every tier ({P['blob_index_tags_10k_month']} EUR per 10 K). Five tags on every part made tiering a net loss at S and M ({R['S']['tk_net5']:+.4f} and {R['M']['tk_net5']:+.4f}). Tagging ticks only with two tags turns it positive ({R['S']['tk_net2']:+.4f} and {R['M']['tk_net2']:+.4f}). Cost: bars are never tiered."
)
w(
    f"* **Private networking.** The optional `enable_private_networking` flag adds EUR {e(PNET_PIP)}/month active (EUR {e(PNET_NAT)} with a NAT gateway) for private endpoints, DNS zones and the VM outbound address. It buys removal of public data-plane endpoints (SPEC-06) and costs several times the whole parked bill, so it is off in dev."
)
w(
    f"* **Defender for SQL off.** Enabling it adds EUR {e(DEFENDER)}/month per server (ADR-0017) and raises the dev log ceiling. Off by default: vulnerability assessment and threat alerts are not running. Production would turn it on."
)
w(
    "* **C2 and C1 file format.** Queries older than 90 days go through DuckDB over Blob (higher first-byte latency, per-query transaction cost); point lookups are slower than a clustered index (T-204 W4); restatements are new versions (ADR-0014); the tiering job is one more moving part."
)
w(
    "* **C10 parking.** Unpark takes minutes to a deploy cycle, jobs do not run while parked, and `terraform destroy` remains the real control for long gaps."
)
w("")

# ---------------------------------------------------------------- cost-model
w("## Cost model re-sum (folds T-506)")
w("")
w(
    "Computed by this script, one row per component of `cost-model.md`. Where the `cost-model.md` row had no formula in the repo it is flagged UNSOURCED."
)
w("")
w("| Component | Active low | Active high | Parked | Basis |")
w("|---|---:|---:|---:|---|")
for n, a, b, c, src in ROWS:
    w(f"| {n} | {e(a)} | {e(b)} | {e(c)} | {src} |")
w(f"| **Total, SQL at list price** | **{e(tot_lo)}** | **{e(tot_hi)}** | **{e(tot_pk)}** | |")
w(
    f"| Total, SQL on the free offer (ADR-0011) | {e(sql_free_lo)} | {e(sql_free_hi)} | {e(sql_free_pk)} | SQL row set to 0 |"
)
w(f"| Parked, `enable_app_service=false` | | | {e(pk_no_app)} | App Service row removed |")
w(
    f"| Parked, AKS deleted (disk, LB, IP released) | | | {e(pk_deleted_aks)} | LB and IP removed, system disk kept (an approximation; the cluster would delete it too) |"
)
w("")
w(
    "The target of EUR 60 active (cost-model.md) is met only with the free SQL offer and disciplined parking; at list price the low end is above it. The previously documented 61-114 / 47 could not be reproduced from the rows (its system-pool high of 35 predates the B2s price in `prices.yaml`)."
)
w("")

# ---------------------------------------------------------------- reproduce
w("## How to reproduce")
w("")
w("```bash")
w("python -I docs/finops/results/c1_c3_model.py                 # rewrites results/c1-c3.md")
w("python -I docs/finops/results/c4_c10_model.py                # prints the C4-C10 tables")
w("python -I docs/finops/deltas/T-404_model.py                  # private networking")
w("python -I docs/finops/deltas/T-506_model.py                  # alerts and blob audit")
w(
    "python -I docs/finops/cost_optimization_report.py            # this report and the cost-model.md totals"
)
w("```")
w("")
w(
    "Needs `pyyaml` and `pyarrow` (both in the uv baseline, ADR-0007). Prices are read from `docs/finops/prices.yaml`; refresh with `finops/fetch_prices.py`."
)
w("")

# ---------------------------------------------------------------- cloud checklist
w("## What turns MODELLED into MEASURED")
w("")
w(
    "Run once in the dev window, then `make cost-report` (T-502, weekly report) and read the named section."
)
w("")
w("| Row | Measurement | Where |")
w("|---|---|---|")
w(
    "| C1, C3 | `BlobCapacity` by `Tier`, bytes per tier after the lifecycle policy acts (up to 48 h) | cost report section 6, [c1-c3.md](results/c1-c3.md) |"
)
w(
    "| C2 | `sp_spaceused` before and after on the Azure database; SQL vCore meter | cost report sections 3 and 8 |"
)
w(
    "| C4 | sweep of 100 children, spot-pool node-hours, evictions, time to first node (needs a subscription-scope export for the `MC_*` group) | [c4-c10.md](results/c4-c10.md) C4 |"
)
w(
    "| C5 | 20 cold requests, p95 time-to-first-byte; `Replicas` metric; Microsoft.App lines | [c4-c10.md](results/c4-c10.md) C5, cost report section 6 |"
)
w(
    "| C6 | `app_cpu_billed`, 10 resume timings with `SELECT 1` and `GET /v1/latest` | [c4-c10.md](results/c4-c10.md) C6 |"
)
w(
    "| C7 | activity-log deallocate/start hours, B1s meter by day | [c4-c10.md](results/c4-c10.md) C7 |"
)
w(
    "| C8 | `Usage` KQL for billable GB per day, cap hits, 24 h with the cap removed for the baseline | [c4-c10.md](results/c4-c10.md) C8 |"
)
w(
    "| C9 | `StorageUsed` before and after `az acr task run -n weekly-purge` | [c4-c10.md](results/c4-c10.md) C9 |"
)
w(
    "| C10 | `make park` / `make unpark`, parked day versus active day | `runbooks/park.md`, cost report section 7 |"
)
w(
    "| All | billed versus list x quantity by `MeterId`; comparison with `cost-model.md` | cost report sections 5 and 8 |"
)
w("")
w("## What would change the conclusions")
w("")
w(
    f"* Serverless active hours above {G13['break_even_hours']:.0f} a month (C2/C6), or a provisioned meter that includes licence (baseline rises, saving rises)."
)
w(
    "* Real log volume far below the assumed uncapped rates (C8 saving shrinks) or above the cap (diagnostics lost)."
)
w(
    "* Spot capacity or eviction rate that stretches sweeps (C4), or `az aks stop` releasing the LB and IP (parked floor falls)."
)
w(
    "* Larger data: at production scale (reservations, savings plans, Hybrid Benefit, dev/test pricing, FinOps governance) the compute rows dominate even more; storage rows stay small until well past L scale."
)
w("")
w("## Numbers not sourced from a script")
w("")
w(
    f"* System pool low end EUR {e(SYSTEM_POOL_LOW_DOC)} and Service Bus EUR {SB_DOC} are carried over from `cost-model.md` as documented inputs (flagged UNSOURCED in the re-sum table)."
)
w(
    "* Scenario assumptions (40 / 150 spot node-hours, 40 active SQL hours, uncapped log rates, image size and cadence) are inputs to the committed scripts, not measurements."
)
w("")

(FIN / "cost-optimization.md").write_text("\n".join(o) + "\n", encoding="utf-8")

# ---------------------------------------------------------------- cost-model.md totals (between markers)
cm = FIN / "cost-model.md"
txt = cm.read_text(encoding="utf-8")
total_row = (
    f"| **Total** | | **≈ {tot_lo:.0f}–{tot_hi:.0f}** (SQL at list; ≈ {sql_free_lo:.0f}–{sql_free_hi:.0f} on the free offer) | "
    f"**≈ {tot_pk:.0f}** (≈ {sql_free_pk:.0f} on the free offer; ≈ {pk_no_app:.0f} with `enable_app_service=false`) | re-summed by `cost_optimization_report.py` (T-507) |"
)
a, b = "<!-- total-row:start -->", "<!-- total-row:end -->"
if a in txt:
    pre, rest = txt.split(a, 1)
    _, post = rest.split(b, 1)
    txt = pre + a + "\n" + total_row + "\n" + b + post
cm.write_text(txt, encoding="utf-8")
print(
    f"wrote {FIN / 'cost-optimization.md'}; totals active {tot_lo:.2f}-{tot_hi:.2f}, parked {tot_pk:.2f}"
)
