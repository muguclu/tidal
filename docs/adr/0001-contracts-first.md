# ADR-0001: Record decisions; contracts-first development
- Status: Proposed · Related: CLAUDE.md §2, PARALLELISM.md
## Context
Work is parallelised across several Claude Code subagents. Without stable interfaces, parallel lanes collide.
## Options
1. Contracts-first: schemas/API/messages/paths frozen before lanes start; fakes for missing parts.
2. Code-first: let interfaces emerge; integrate later.
## Decision
Option 1. It enables safe parallelism and gives reviewers a reference for every PR.
## Consequences
Phase 0 is slower. Contract changes need ADRs. Contract tests keep docs and code in sync.
