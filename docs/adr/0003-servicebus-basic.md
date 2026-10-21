# ADR-0003: Service Bus Basic queues
- Status: Proposed · Related: messaging-contract §1
## Context
Need job queues for backtests and tiering; topics only nice-to-have.
## Options
1. Basic: queues only, cheapest; no topics, no sessions.
2. Standard: topics/sessions, fixed monthly base charge.
## Decision
Basic. Concurrency control via SQL applock instead of sessions. Topic behind a flag.
## Consequences
No pub/sub fan-out by default; message size limit lower than Premium; documented upgrade path.
