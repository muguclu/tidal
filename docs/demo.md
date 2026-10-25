# Demo: 3-minute video script (T-603)

Audience: a hiring manager or front-office tech lead. Length: 3:00 or less. Everything runs on one laptop.
Nothing is deployed to Azure, and the video says so in the first 40 seconds.

Rules for this script:

* Every number on screen is copied from [`docs/finops/cost-optimization.md`](finops/cost-optimization.md), with its label
  (MEASURED or MODELLED). No other figure is shown.
* No secret is shown. The only credentials in the stack are the public Azurite and Service Bus emulator values and a
  fake SQL password from `.env.example`. Do not open `.env` on screen.
* Use a bash shell (Git Bash or WSL on Windows). The commands below use bash syntax.

## Pre-flight checklist (do this before recording)

Run it once end to end the day before. Clock 2026-10-25 or later.

1. Docker Desktop is running. Python 3.12 and `uv` are installed.
2. `uv sync` has finished.
3. `cp .env.example .env`, then set your own `TIDAL_SQL__PASSWORD` in `.env`. Keep `TIDAL_AUTH__DISABLED=true`.
4. Pull and build once, so the recording does not wait on downloads: `docker compose --profile app build` and
   `docker compose pull`.
5. Rehearse the full command list below on a clean volume (`docker compose down -v`), then bring the stack down again.
   The first run is the slow one. Time the synth step: the committed reports show about 105 s for `daily-history`
   including container start (`docs/status/2026-W43-G1-synthetic.md`). Pre-record or speed up that wait; do not talk
   over a silent screen.
6. Rehearse segment 7 once: the worker runs inside the `api` container and processes one job per
   `--max-messages`. Leave the first rehearsal job processed so the queue is empty when you record.
7. Terminal: font size 18 or larger, 100 columns, dark theme, history cleared, no tokens or `.env` in the scrollback.
8. Browser tabs ready: `docs/finops/cost-optimization.md` (rendered, scrolled to the "Register C1 to C10" table) and
   `docs/img/architecture.md` (rendered Mermaid diagram).
9. Close mail, chat and anything with notifications.

## Setup commands used in the video

```bash
# segment 3: start the local stack (infra + api + query)
docker compose --profile app up -d
docker compose ps
curl -s http://127.0.0.1:8000/v1/health/live

# segment 4: create the schema, then synthetic data (seeded, idempotent)
# The CLI runs inside the api container: it already has the in-network endpoints and ODBC Driver 18.
# Running `uv run tidal ...` on the host also works, but needs ODBC Driver 18 installed and .env exported.
docker compose exec api tidal db migrate
docker compose exec api tidal synth daily-history

# segment 5: query the API (auth is disabled in compose, so no token)
curl -s "http://127.0.0.1:8000/v1/latest?instrument_id=EIA.BRENT.SPOT&interval=1d"
curl -si "http://127.0.0.1:8000/v1/bars?instrument_id=EIA.WTI.SPOT&interval=1d&start=2020-04-17T00:00:00Z&end=2020-04-23T00:00:00Z"

# segment 6: tiering (dry run first, frozen clock so the window is repeatable)
docker compose exec -e TIDAL_CLOCK__NOW=2027-01-31T00:00:00Z api tidal tiering run --dry-run
docker compose exec -e TIDAL_CLOCK__NOW=2027-01-31T00:00:00Z api tidal tiering run

# segment 7: submit a backtest
curl -s -X POST http://127.0.0.1:8000/v1/backtests \
  -H "Content-Type: application/json" \
  -d '{"strategy":"ma_crossover","instrument_ids":["EIA.BRENT.SPOT"],"params":{"fast":10,"slow":50},
       "interval":"1d","start":"2015-01-01T00:00:00Z","end":"2020-01-01T00:00:00Z",
       "costs":{"fee_bps":1,"slippage_bps":2},"seed":1}'
# run one worker pass (in Azure, KEDA starts these on spot nodes from zero)
docker compose exec api tidal-worker --queue backtest-jobs --max-messages 1 --max-wait 30
# then, with the job_id from the 202 response (the in-API aggregator applies the result within seconds):
curl -s http://127.0.0.1:8000/v1/backtests/<job_id>

# teardown after recording
docker compose --profile app down
```

Route and parameter sources: `docs/contracts/openapi.yaml`. `/v1/latest` needs `instrument_id` (repeatable) and takes
`interval` (default `1d`). `/v1/bars` needs `instrument_id`, `interval`, `start`, `end`. `POST /v1/backtests` needs
`strategy`, `instrument_ids`, `params`, `interval`, `start`, `end`, `costs` (`fee_bps`, `slippage_bps`) and `seed`;
`ma_crossover` takes `fast` and `slow`. The `bars` response carries the `X-Tidal-Tier` header, which is why the bars
call uses `curl -si` (prints headers).

### Dev token (real JWT path), not part of the 3 minutes

The compose stack runs with `TIDAL_AUTH__DISABLED=true`, which is local only (startup asserts `TIDAL_ENV=local`).
To show the real validator, set the commented auth block in `.env.example` (`TIDAL_AUTH__DISABLED=false`, the dev
`TIDAL_AUTH__JWKS_URL`, tenant and client ids), restart the API, and mint a token without printing it:

```bash
TOKEN=$(uv run python scripts/dev-token.py --role Tidal.Quant)   # signed with a local test key, valid only locally
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/v1/latest?instrument_id=EIA.BRENT.SPOT"
```

Never `echo $TOKEN` on camera. The roles are `Tidal.Reader`, `Tidal.Trader`, `Tidal.Quant`, `Tidal.Admin`
(`POST /v1/backtests` needs `Tidal.Quant`). Real Entra ID has not been exercised (NEEDS-CLOUD).

## Script

Spoken pace is about 150 words per minute. The voice-over below is about 415 words, so it fits with room for pauses.

| # | Time | On screen | Voice-over |
|---|---|---|---|
| 1 | 0:00 to 0:20 | Title card, then the README top: "Tidal: a cost-optimised time-series platform for front-office trading, on Azure". | This is Tidal. It is a time-series platform for energy and commodity trading data, designed for Azure. The goal is simple. Every cost claim has a number behind it. I will show what runs on my laptop today, and what does not yet. |
| 2 | 0:20 to 0:40 | Architecture diagram (`docs/img/architecture.md`). Overlay text: "Nothing is deployed to Azure. All data is synthetic." Pan from storage to serving to backtests. | One thing first. Nothing is deployed to Azure. The Terraform and Helm are written and checked in CI, but the cloud figures are modelled, not billed. Locally, a blob emulator stands in for Storage, a SQL Server container for Azure SQL, and an emulator for Service Bus. |
| 3 | 0:40 to 1:05 | Terminal: `docker compose --profile app up -d`, `docker compose ps` (services healthy), then the `curl` to `/v1/health/live`. Overlay: "Auth off: local only". | One command starts the stack, plus the API and query service. Everything reports healthy. Authentication is switched off here, and the app refuses that setting outside a local environment. The real token check is tested separately, against a local key set. |
| 4 | 1:05 to 1:30 | Terminal: `docker compose exec api tidal db migrate`, then `docker compose exec api tidal synth daily-history` (sped up). Show the log lines at the end. | Now data. All of it is synthetic and seeded, so every run gives the same bytes. This command builds twenty years of daily history for twelve series. Recent bars land in SQL, the hot tier. Older bars are written as epoch Parquet files in blob storage, the cold tier. |
| 5 | 1:30 to 1:55 | Terminal: `curl` `/v1/latest` for Brent. Then `curl -si` `/v1/bars` for WTI, 17 to 23 April 2020; highlight the `X-Tidal-Tier` header and the negative close on 2020-04-20. | Ask for the latest Brent bar. That comes from the hot tier. Now ask for WTI around April 2020. The price is negative, about minus thirty-seven, and the system never clamps it. This range comes from cold storage, and the tier header says so. |
| 6 | 1:55 to 2:15 | Terminal: the two `tidal tiering run` commands. Then the C2 row from the cost report: "MEASURED (local): `md.tick_hot` reserved KB 71,368 to 2,184". | Tiering moves rows older than the ninety-day hot window from SQL to Parquet. Dry run first. It only prints the plan. On the real local run, the tick table fell from seventy-one thousand to two thousand kilobytes. That is measured, on this laptop. |
| 7 | 2:15 to 2:35 | Terminal: `POST /v1/backtests` → `202` with `job_id`; one `tidal-worker` pass; `GET /v1/backtests/<job_id>` → `"status":"succeeded"` with Sharpe and trade count. Back to the diagram, highlight Service Bus, KEDA ScaledJob and the spot pool. Overlay: "Scale 0 to N: NEEDS-CLOUD". | Backtests are queued, not run inside the API. I post a moving average crossover on Brent and get a job id back. A worker picks it up, writes the equity curve to blob storage and reports the result. Same seed, same result hash, every time. In Azure, KEDA would start those workers on spot nodes from zero; that scaling has not run yet. |
| 8 | 2:35 to 3:00 | Cost report: the Register C1 to C10 table. Highlight in turn: C1 `98.35 -> 27.10 MiB`, `72.4 %`, MEASURED; C4 `667.95 -> 1.63 / 6.12`, MODELLED, NEEDS-CLOUD; the totals `68.40 to 126.86` active and `46.61` parked, MODELLED. End card: repo name and "docs/finops/cost-optimization.md". | This is the cost report. The epoch Parquet format is seventy-two percent smaller than SQL rows. That is measured. The big savings are modelled: backtest nodes on spot machines, six hundred sixty-eight euros down to about two to six. A month is modelled at sixty-eight to one hundred twenty-seven euros active, forty-seven parked. These are not invoices. A short cloud window would turn them into measurements. Thanks for watching. |

Voice-over word count by segment (hand count, plus or minus 5 in total): 1 = 46, 2 = 49, 3 = 47, 4 = 48, 5 = 48,
6 = 48, 7 = 56, 8 = 73. Total about 415, under the 450 limit.

Numbers used on screen and their source rows in `docs/finops/cost-optimization.md`:

| Shown | Value | Label in the report |
|---|---|---|
| C1 bytes at rest, S-3m slice (2,243,908 ticks) | 98.35 -> 27.10 MiB, 72.4 % | MEASURED |
| C2 `md.tick_hot` reserved KB (local) | 71,368 -> 2,184, 96.9 % | MEASURED (local) |
| C4 compute, EUR/month | 667.95 -> 1.63 / 6.12 | MODELLED, NEEDS-CLOUD |
| Total, SQL at list price, EUR/month | 68.40 active low, 126.86 active high, 46.61 parked | MODELLED |

The "about minus thirty-seven" for WTI on 2020-04-20 is the golden test value in
`docs/status/2026-W43-G1-synthetic.md` (spot -37.0), not a cost figure. Show the actual value from your response.

## Segment notes

Segment 5. `/v1/bars` for 2020 should be served from cold storage (older than the 90-day hot window), so `X-Tidal-Tier`
should read cold. If the header says something else, say what it says. Do not narrate the expected value.

Segment 6. The frozen clock `2027-01-31` is later than the data clock used by `synth`, so the plan has rows to move.
The 71,368 to 2,184 figure in the report came from the S-scale tick data and a separate run, not from `daily-history`
(which writes bars only). Say "the report's measured run", and point at the table; do not claim this run produced it.
If the dry run says nothing to move, show that output and go to the table.

Segment 7. The worker runs inside the `api` container, so it reaches `tidal-query` on the compose network
(`TIDAL_QUERY__BASE_URL=http://query:8000`); nothing needs to be published. On the dry run (2026-10-25) the job went from
`queued` to `succeeded` about 12 seconds after the worker pass (Sharpe 0.593, 30 trades), and a second identical
request produced the same `result_sha256`. Show the real response, not these values.

## Fallback if a step fails live

* Stack does not come up: show `docker compose ps` and `docker compose logs --tail 30 <service>`. The usual cause is a
  missing or weak `TIDAL_SQL__PASSWORD`, or an old SQL volume with a different password (`docker compose down -v`). If it
  does not clear in 30 seconds, cut to the pre-recorded take of segments 3 to 7 and say so in the voice-over.
* Synth is slow: cut the wait in editing (it is idempotent, so a second run returns quickly).
* API returns 401 or 403: auth is probably enabled. Check `TIDAL_AUTH__DISABLED=true` in `.env` and restart the `api`
  container. Do not paste a token on screen to work around it.
* Bars or latest return 404 or empty: the synth step did not finish. Re-run `docker compose exec api tidal synth daily-history`.
* Backtest returns 503: the Service Bus emulator is not ready (it starts after SQL Server). Wait 20 seconds, retry. If
  it still fails, skip to segment 8 and state that the submit path is covered by the contract and unit tests
  (`tests/contract/test_backtests_contract.py`, `tests/unit/api/test_backtests.py`).
* Backtest stays `queued`: the worker pass found no message (run it again) or the result is waiting for the
  aggregator; check `docker compose logs --tail 30 api` for `result_applied` or `aggregator_failed`.
* Anything else: show the cost report and the diagram only (segments 1, 2, 8). They need no running stack and carry the
  main message.

## Dry-run record (2026-10-25, local compose stack)

Every command in "Setup commands used in the video" was run once by the orchestrator:

* `docker compose --profile app up -d`, `/v1/health/live` and `/v1/health/ready`: OK.
* `tidal db migrate` (no-op on an existing schema) and `tidal synth daily-history`: 12 instruments.
* `/v1/latest` for Brent: `"tier":"hot"`. `/v1/bars` for WTI 2020-04-17 to 2020-04-23: `x-tidal-tier: cold`, close -37.0
  on 2020-04-20.
* `tidal tiering run --dry-run` with the frozen clock: `md.bar_hot: moved=780`, `md.tick_hot: moved=0`.
* Backtest: 202, worker pass, then `succeeded` with metrics; resubmitting gave an identical `result_sha256`.

The dry run exposed three gaps that unit tests with fakes had hidden. All are fixed:

* T-606 (#96): the API had no Service Bus producer.
* T-607 (#98): there was no SQL job store and the aggregator never ran.
* T-607 (#98): Azurite had no `backtests` container.

Not run: `scripts/dev-token.py` (the JWT path in "Not in the 3 minutes"), and the recording itself.
