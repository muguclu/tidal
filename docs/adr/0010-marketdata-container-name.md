# ADR-0010: Market-data Blob container is named `marketdata`

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ orchestrator, from the T-106 integration run)
- Related: storage-layout §1–2, SPEC-03, SPEC-06 §2, ADR-0004, T-106, T-121, T-207

## Context
storage-layout v1 named the market-data container `md`. Azure container names must be 3–63 characters, so
`create_container("md")` fails (`OutOfRangeInput`) on Azurite and on real Blob. Found by the first T-106
integration run against Azurite; nothing has been written to Azure yet.

## Options
1. **`marketdata`** — self-explanatory, valid, paths read `marketdata/v1/ticks/...`. € impact: none.
2. **`mdata` / `md1`** — shorter paths, less readable; saves a few bytes per blob name. € impact: none.

## Decision
Option 1: the container is `marketdata`; the SQL schema stays `md` (SQL has no length rule here).

## Consequences
`layout.CONTAINER_MD`, its path regex, tests, lifecycle filters (T-207), RBAC scope (T-403) and the storage
module (T-121) use `marketdata`. v1.1 clarification of a contract that could never have been instantiated; no
data migration.
