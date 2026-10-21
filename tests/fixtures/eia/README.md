# EIA v2 fixtures

Hand-written, "recorded-style" responses following the EIA Open Data API v2 `data` endpoint
shape (`response.total` / `response.data[]` with string `value`s). No real recordings exist
because no API key was available (T-102); replace with vcrpy cassettes once a key is.

`rwtc_page1.json` / `rwtc_page2.json`: two pages (total 6, three rows each) including the
2020-04-20 negative WTI row. The value -37.63 follows the task brief (the NYMEX settle); the EIA
RWTC spot print for that day is believed to be -36.98 (unverified).
