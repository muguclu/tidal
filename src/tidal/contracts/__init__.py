"""Contract code mirror of ``docs/contracts/`` (ADR-0001).

- ``schemas``: arrow schemas, quality flags, interval codes, Parquet encodings.
- ``instruments``: instrument model, ``instrument_id`` regex, v1 catalogue (data-contracts §1).
- ``models``: API request/response models (api-contract §3).
- ``messages``: Service Bus envelopes and payloads (messaging-contract §2-3).
- ``export``: ``python -m tidal.contracts.export`` writes ``docs/contracts/schemas/*.json``.
"""
