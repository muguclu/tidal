# EIA v2 fixtures

Hand-written, "recorded-style" responses following the EIA Open Data API v2 `data` endpoint
shape (`response.total` / `response.data[]` with string `value`s). No real recordings exist
because no API key was available (T-102); replace with vcrpy cassettes once a key is.

`rwtc_page1.json` / `rwtc_page2.json`: two RWTC pages (total 6) incl. 2020-04-20 = -36.98 (EIA spot).
`rclc1_page1.json`: RCLC1 (NYMEX contract 1) 2020-04-20 = -37.63. Live recordings (via
`scripts/verify_eia.py --record`) go in `live/`.
