# KICKOFF — Starting Tidal with Claude Code

## 0. Prerequisites (human, once)
- GitHub repo `tidal` (public), second GitHub account for reviews (SPEC-08 §1 solo note).
- Azure subscription with a budget; `az login`; Terraform ≥ 1.9; Docker; uv; Node (for oasdiff/redocly via npx).
- Free API keys: EIA (eia.gov/opendata), FRED (fred.stlouisfed.org).
- This folder is the repo root (`git init -b main`), first commit `chore: planning docs (T-000)`.
- Windows: `winget install ezwinports.make Hashicorp.Terraform Microsoft.AzureCLI` (make is needed
  for the Makefile targets; CI runs on Linux).

## 1. First session — review the plan (no code)
```
Read CLAUDE.md, docs/PLANNING.md, docs/PARALLELISM.md, docs/tasks/TASKS.md, all specs and contracts.
Act as the architect agent. List inconsistencies, missing decisions and risky assumptions as a table
(file, issue, proposed fix). Do not change files yet.
```
Apply the fixes you agree with, then commit.

## 2. Phase 0
```
/next-task T-001
```
Implement T-001 in the main session (it is the scaffold everything else needs). Then:
```
/wave 0
```
This proposes W0b (T-003/T-004/T-002) and then W0c (T-006/T-008) — at most 3 lanes at once. Confirm, let agents run, read reviews,
open PRs, merge in order. Then:
```
/gate G0
```
Tag `contracts-v1` when G0 passes.

## 3. Each later phase
```
/wave <n>   → confirm lanes → agents implement → reviewers review → you open PRs & merge
/gate G<n>  → fix gaps → next wave
```
Human-only steps (never delegated): `terraform apply`, first `helm upgrade`, Entra admin consent,
screenshots for evidence, merging PRs, recording the demo.

## 4. Session hygiene
- Start every session with: "Read CLAUDE.md and TASKS.md; tell me the current phase, open tasks and blockers."
- Keep `TASKS.md` statuses current — it is the shared memory between sessions and agents.
- After each wave, ask for a 5-line status note in `docs/status/`.
- Run `make park` at the end of every working day once Azure resources exist.
