# ADR-0001: Record architecture decisions; contracts-first development

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ architect agent)
- Related: [CLAUDE.md §2.1](../../CLAUDE.md), [PARALLELISM §1](../PARALLELISM.md),
  [data-contracts §8](../contracts/data-contracts.md), [plan review 2026-10-21](../reviews/2026-10-21-plan-review.md), T-003, T-004, T-006

## Context
Tidal is built by one person and several AI agents working in parallel lanes (up to 3 at once). If the
data shapes, API, queue messages and storage paths are not fixed before the lanes start, one lane's
change silently breaks another, and a reviewer has nothing to check a PR against. The job requires
visible **engineering practices** (ADRs, contract tests, reviews) and clear **communication**: a hiring
manager should be able to see *why* each decision was made, not just the code.

## Options
1. **Contracts-first + ADRs.** Schemas, API (OpenAPI), messages, storage paths and config keys are written
   in `docs/contracts/` and mirrored in `src/tidal/contracts/` before any wave starts; lanes use fakes for
   parts that do not exist yet; every significant or breaking change gets a short ADR.
   - Pros: safe parallel work; every PR has a reference; contract tests catch doc/code drift.
   - Cons: Phase 0 takes longer; contract changes carry paperwork (ADR + `contract-change` label).
   - € impact: no Azure cost; the cost is Phase 0 engineering time.
2. **Code-first.** Let interfaces emerge from the code and integrate at the end of each wave.
   - Pros: faster start; no up-front design documents.
   - Cons: merge conflicts and rework between lanes; reviewers cannot judge correctness; decisions are
     lost in commit history.
   - € impact: no Azure cost; rework time is not measurable in advance.

## Decision
Option 1: freeze contracts before parallel work and record every significant decision as an ADR,
because that is what makes several agents safe to run at once and gives reviewers a fixed reference.

## Consequences
- Easier: parallel lanes, PR review, onboarding a reader via `docs/adr/README.md`.
- Harder: every contract change follows data-contracts §8 (minor = additive, no ADR; major = ADR and
  both versions readable for one phase).
- Monitor: contract tests (`tests/contract/`) and `oasdiff breaking` in CI stay green; count of
  contract-change PRs per wave (many = the contract was frozen too early).
- Revisit if contract churn blocks more than one wave, or when the project gains real consumers outside
  this repo (then consider published, versioned schema packages).
