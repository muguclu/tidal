# Load tests (T-504)

`locustfile.py` drives `GET /v1/bars|ticks|latest|spreads` and `POST /v1/backtests`. It is not a pytest test
and is never collected by `uv run pytest`.

```bash
docker compose --profile app up -d          # azurite, sqlserver, servicebus, query, api (auth disabled)
uv run --group load locust -f tests/load/locustfile.py --headless -u 5 -r 1 -t 90s \
    --host http://127.0.0.1:8000 --csv docs/finops/results/load/local-run
```

- Token: `TIDAL_LOAD_TOKEN` (bearer); empty works only when the API runs with `TIDAL_AUTH__DISABLED=true`.
  Mint a dev token with `scripts/dev-token.py`. Never commit a token.
- `TIDAL_LOAD_BACKTEST_WEIGHT=0` disables backtest submit. The local compose api has no Service Bus
  wiring for submit (`503 The message bus is not configured`), so local runs set it to 0.
- `TIDAL_LOAD_PROFILE=cold` widens bars/spreads to 2005-onwards (cold tier).
- Local results are single-machine numbers against Docker Compose: a reference for the C5 baseline, not Azure.
  Cloud runs against Container Apps use the same file with `--host https://<api fqdn>` and a real token.
- Runs are capped at 3 minutes; keep user counts small on a laptop.
