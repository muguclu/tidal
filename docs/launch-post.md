# LinkedIn launch post (T-605)

Draft. Replace `<repo-url>` and `<demo-video-url>` before posting. Every number is copied from
`docs/finops/cost-optimization.md`. Nothing is deployed to Azure, so no figure is an invoice.

## Hook options (pick one as the first line)

1. Most of the savings in my trading-data platform came from compute that is switched off, not from clever storage.
2. I stored the same market ticks two ways. The smaller one saves cents. The queue-driven backtest nodes save hundreds.
3. My hand-typed cost total did not add up. A script found it in seconds.

## Main post

I built a time-series platform for front-office trading data, then priced every design choice. It is not deployed to Azure yet, and I say so below.

The problem: energy and commodity desks keep 20+ years of price history. Holding all of it on a hot database is costly, and backtests need many machines for an hour, then none.

What I built (Tidal, a portfolio project on Azure):
- Epoch Parquet storage with hot/cold tiering from SQL to Blob
- An Entra ID secured API
- Backtests as queue-driven jobs on AKS spot nodes, scaled 0 to N with KEDA
- Terraform for everything, GitHub Actions with OIDC, no secrets in code

All data is synthetic, including a negative WTI price on 2020-04-20.

Results, with labels:
- Measured locally: Parquet is 27.10 MiB against 98.35 MiB for a SQL rowstore (72.4 % smaller). Real, but worth cents a month.
- Modelled from Azure list prices: backtest nodes that exist only during a sweep, EUR 667.95 down to EUR 1.63-6.12 a month (99.8 % to 99.1 % less).
- Modelled: a database that pauses when idle, EUR 218.91 down to EUR 13.95-44.25.
- Modelled: a month with about 40 hours of use costs EUR 68.40 to 126.86. This is not an invoice.

The assumptions behind each saving, and the costs (cold starts, spot evictions), are in the report.

Lesson: re-summing the cost model by script showed my hand-typed total did not add up. Running the demo end to end found a missing message-bus producer that unit tests with fakes had hidden. Check totals by code, and test across the real boundary.

Code, specs, ADRs and the cost report: <repo-url>
3-minute demo: <demo-video-url>

## Short version (about 600 characters)

I built Tidal, a time-series platform for trading data on Azure, and priced every design choice. Synthetic data, not deployed yet.

Modelled from list prices: backtest nodes that exist only during a sweep cut compute from EUR 667.95 to EUR 1.63-6.12 a month. Measured locally: Parquet is 72.4 % smaller than a SQL rowstore, but that is worth cents.

Lesson: the big savings come from compute that is switched off.

Repo: <repo-url>
Demo: <demo-video-url>

## Hashtags

#FinOps #Azure #Kubernetes #TimeSeries #EnergyTrading #QuantitativeFinance #Terraform #DataEngineering

## Number sources (all in `docs/finops/cost-optimization.md`)

| Number in post | Source |
|---|---|
| 27.10 vs 98.35 MiB, 72.4 %, MEASURED | register row C1 (line 19) |
| 667.95 -> 1.63 / 6.12, 99.8 % / 99.1 %, MODELLED | register row C4 (line 24); summary line 8 |
| 218.91 -> 13.95 - 44.25, MODELLED | register row C6 (line 26); summary line 8 |
| EUR 68.40 to 126.86 for about 40 h, MODELLED | summary line 7; re-sum table line 126 |
| "worth cents" | summary line 9 |
| Hand-typed total did not add up | line 131; `docs/status/2026-W43-G5.md` line 20 |
| Negative WTI 2020-04-20, synthetic data | README intro; ADR-0013 |
