# ADR-0005: Trunk-based development with short-lived branches

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ architect agent)
- Related: [SPEC-08 §1, §3](../specs/SPEC-08-cicd-engineering.md), [PARALLELISM §1, §4](../PARALLELISM.md),
  [engineering-practices](../engineering-practices.md), T-002, T-007

## Context
The team is one human plus AI agents, delivering continuously to a single `dev` environment, with up to
three agents working in parallel worktrees. The job requires visible **engineering practices**: branch
protection, PR reviews and CI gates. The branching model decides how often parallel work collides and how
much review history a reader can browse.

## Options
1. **Trunk-based.** Short-lived `type/T-xxx-slug` branches (< 3 days), one task per branch, squash merge
   to a protected `main`, release tags `vX.Y.Z`.
   - Pros: few long-lived branches, so few merge conflicts between agent worktrees; linear history;
     every change is a reviewed PR.
   - Cons: needs strong CI gates and feature flags because `main` must always be deployable.
   - € impact: no Azure cost difference.
2. **GitFlow.** `develop`, `release/*`, `hotfix/*` branches next to `main`.
   - Pros: fits a regulated release train with several supported versions.
   - Cons: long-lived branches drift; double merges; more conflicts for parallel agents; no benefit with
     one environment and one supported version.
   - € impact: no Azure cost difference.

## Decision
Option 1, because short-lived branches into one protected trunk keep parallel agents from colliding and
match a single-environment continuous-delivery setup.

## Consequences
- Required checks on `main`: lint, types, tests, coverage, contracts, openapi-diff, secrets-scan,
  review-checklist, plus iac and image-scan when those areas change (SPEC-08 §3).
- The orchestrator pushes branches, opens PRs and squash-merges them in the fixed wave order once CI is
  green and reviews have no open `blocking:` findings (owner decision 2026-10-21). Subagents never merge.
- Solo developer: GitHub does not allow self-approval, so the review mechanism (second account or
  `review-checklist` check) is documented transparently in the README (SPEC-08 §1).
- Monitor: branch age, PR size (< ~400 changed lines), number of rebases per wave.
- Revisit if Tidal ever needs several supported versions at once; the README explains when GitFlow fits.
