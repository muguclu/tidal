# Plan review — 2026-10-21 (KICKOFF §1)

Reviewer: main session acting as `architect`. Scope: CLAUDE.md, PLANNING, PARALLELISM, TASKS, SPEC-01…10,
all contracts, ADRs, agents. All 31 findings accepted by the owner and applied in the same commit.

| # | Severity | File(s) | Issue | Fix applied in |
|---|---|---|---|---|
| 1 | critical | repo | Planning folder lived inside an unrelated Desktop repo | own repo, `git init -b main`; KICKOFF §0 |
| 2 | critical | TASKS T-001 | `pyproject`/`uv.lock`/`Makefile`/`conftest` would be edited by every parallel lane | T-001 owns full set; PARALLELISM §1.7 |
| 3 | critical | CLAUDE §3, specs | Specs need deps not in the stack (numpy, PyJWT, schemathesis, vcrpy, …) | ADR-0007; threat model → ADR-0008 |
| 4 | critical | PLANNING G0/G1, LOCAL-DEV | Gates required Azure while Phases 0–3 are "no Azure" | `NEEDS-CLOUD` marker; T-008, T-126 |
| 5 | critical | storage-layout §2, SPEC-03 §5 | Compaction dropped old versions (breaks `as_of`); reader rule compared version to time | data-contracts §7; storage-layout §2; SPEC-03 §2, §3.1, §5 |
| 6 | critical | data-contracts §3/§6 | SQL `FLOAT` cannot hold NaN volume | data-contracts §3.1 |
| 7 | critical | data-contracts §6 | DDL incomplete (`tick_hot`, `ref.instrument`, `ops.ingest_checkpoint`, `ops.backtest_job`) | data-contracts §6 |
| 8 | critical | SPEC-06 §2 | `id-worker` had no SQL read but the reader reads the hot window | SPEC-06 §2 |
| 9 | critical | SPEC-06 §2 | "Tag Contributor" is an ARM role; does not cover blob index tags | SPEC-06 §2 |
| 10 | critical | SPEC-06 §2, SPEC-08 §2 | CI SP cannot `terraform apply`; deploy.yml applied infra (human-only) | SPEC-08 deploy.yml; SPEC-09 §2 RG export |
| 11 | critical | SPEC-05/07, LOCAL-DEV | Free trial quota/spot ban makes the 20-pod demo impossible | LOCAL-DEV §2 PAYG+quota; PLANNING §7; SPEC-05 §5 |
| 12 | critical | SPEC-04 §1 | Internal Container Apps ingress unreachable from App Service without VNet | SPEC-04 §1 |
| 13 | gap | TASKS | Unowned: park/unpark, dev-token, sb-config, dlq CLI, iac/bench/cost workflows, JSON schemas | T-113, T-127, T-313; T-112/T-204/T-303/T-502/T-003 Owns |
| 14 | gap | SPEC-01/02 | No tick→bar aggregation, yet backtests use 1m bars | T-107; data-contracts §3.2 |
| 15 | gap | data-contracts §1 | No canonical instrument catalogue | data-contracts §1.1 |
| 16 | gap | PLANNING §2, SPEC-05 | Calendar spread/curves need futures; legs typed as numeric params | EIA FUT1–4; api-contract; SPEC-05 §2 |
| 17 | gap | TASKS | `cost-model.md` edited by several W3 lanes | `docs/finops/deltas/`; CLAUDE §2.5 |
| 18 | gap | config-contract | Missing local connection strings, SQL user, query URL/audience, service name | config-contract |
| 19 | gap | SPEC-01 §3 | Router used wall clock; synthetic range ends before "now" | `Clock`, `TIDAL_CLOCK__NOW`, `TIDAL_SYNTH__START/END` |
| 20 | gap | SPEC-03, storage-layout | Monthly migration into yearly daily-bar partitions undefined | storage-layout §2; SPEC-03 §2 |
| 21 | gap | api-contract | Role hierarchy undefined | api-contract §1.1 |
| 22 | gap | SPEC-07 §2 | VM self-deallocate role + scheduler resource missing | SPEC-06 §2; SPEC-07 §2; cost-model |
| 23 | risk | SPEC-07 §4 | < 250 MB image unrealistic with pyarrow+polars+duckdb+ODBC | measure, ≤ 500 MB |
| 24 | risk | storage-layout §2 | 64–256 MB target vs 1M-row roll (~5–10 MB) | monthly files |
| 25 | risk | SPEC-03 §1, PLANNING §3 | "→ Archive" for md contradicts ADR-0004 | wording fixed |
| 26 | risk | SPEC-02 §5 | Free SQL offer too small for M-scale SQL benchmark | S-scale Azure SQL + modelled M |
| 27 | risk | messaging §4 | Basic SKU has no de-dup; results redelivered | idempotent aggregator; T-304 |
| 28 | risk | PLANNING G1, SPEC-04 | "OpenAPI diff = 0" unattainable with FastAPI output | `oasdiff breaking` both directions |
| 29 | risk | PARALLELISM §2 | W0b had 5 lanes vs max 3 | W0b/W0c split |
| 30 | risk | env | `make`/`terraform`/`az` missing on Windows dev box | KICKOFF §0 winget line |
| 31 | risk | PLANNING §5 | ~75 tasks in 100 h | minimum-hireable cut is primary target |
