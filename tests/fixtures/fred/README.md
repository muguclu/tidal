# FRED fixtures

Hand-written responses in the FRED `series/observations` JSON shape (`count`, `observations[]`
with string `value`s, `"."` = missing). Dates start at 1999-01-04 (first DEXUSEU observation);
the values are illustrative, NOT recorded. No API key was available (T-103); live recordings via
`scripts/verify_fred.py --record` go in `live/`. Raw payload = verbatim response minus any
`api_key` echo.
