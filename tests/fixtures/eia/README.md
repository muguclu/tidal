# EIA v2 fixtures

Hand-written, "recorded-style" responses following the EIA Open Data API v2 `data` endpoint
shape (`response.total` / `response.data[]` with string `value`s). No real recordings exist
because no API key was available (T-102); replace with vcrpy cassettes once a key is.

`rwtc_page1.json` / `rwtc_page2.json`: two RWTC pages (total 6) incl. 2020-04-20 = -36.98 (EIA spot).
`rclc1_page1.json`: RCLC1 (NYMEX contract 1) 2020-04-20 = -37.63. Live recordings (via
`scripts/verify_eia.py --record`) go in `live/`.

Status of "recorded HTTP fixtures (vcr)": waived by the orchestrator until an API key exists. The
hand-made fixtures plus `verify_eia.py --record` (unit-tested) stand in; the live recording is a
NEEDS-KEY follow-up before gate G1. vcrpy is not used. Raw payload = verbatim response minus any
`api_key` echo in `request.params`.
