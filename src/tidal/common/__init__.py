"""Cross-cutting utilities shared by every service.

- ``config``: ``Settings`` mirroring ``docs/contracts/config-contract.md``; ``get_settings()``.
- ``errors``: typed ``TidalError`` hierarchy with HTTP status and problem ``type`` URI.
- ``logging``: structlog JSON to stdout, correlation ids, secret redaction (SPEC-10 §1).
"""
