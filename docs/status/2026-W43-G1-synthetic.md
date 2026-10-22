# Status 2026-W43 — G1 item 1 closed on synthetic data (ADR-0013)

Supersedes the **NEEDS-KEY** row in [2026-W43-G1.md](2026-W43-G1.md). The project no longer uses vendor API keys; T-117 (#45) removed `TIDAL_SOURCES__*` and the real-source CLI, and added `tidal synth daily-history`.

| Gate item | Result | Evidence |
|---|---|---|
| 20+ years of synthetic daily history ingested locally into Parquet + SQL Server container | PASS | `tidal synth daily-history` (default start 2005-01-01, default seed) in the dev image against compose Azurite + SQL Server 2022, system clock 2026-10-22: 12 series, **756 hot + 67,500 cold** bars, 105 s wall incl. container start. Re-run: 0 / 0 rows, 17 s (idempotent). Unit tests: byte-identical output per seed, range independence, WTI 2020-04-20 spot −37.0 / FUT1 −37.6 golden |

**G1 overall:** items 1, 2, 4 PASS; item 3 (`terraform` plan/apply) remains NEEDS-CLOUD for the cloud window.
**Also merged:** #44 `LocalStore.list` walks only the prefix directory (review finding from T-117).
**Next:** resume Wave 2 — T-202/T-203 → T-204, T-205–T-207, T-209/T-210.
