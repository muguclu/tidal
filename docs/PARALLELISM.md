# PARALLELISM — How Claude Code runs this project in parallel

The main Claude Code session is the **orchestrator**. It never writes feature code itself; it picks
tasks, spawns subagents, reviews their output, merges in order and checks gates.

## 1. Principles
1. **Contracts before parallelism.** Lanes only start after `contracts-v1` is tagged (G0). Lanes talk
   through contracts and fakes, never through each other's unfinished code.
2. **File ownership.** A task may only touch the files in its `Owns` column (TASKS.md). Two tasks in the
   same wave never own the same file. The orchestrator checks this before spawning.
3. **One worktree per lane.** The orchestrator creates them from the Tidal repo before spawning:
   `git worktree add ../tidal-<lane> -b <branch> main`, and the brief names the absolute path. Do not use
   the Agent tool's `isolation: "worktree"` here — it resolves to the parent Desktop repo (W0b lesson).
4. **Max 3 implementing agents at once** + reviewers. More causes review bottlenecks, not speed.
5. **Merge order is fixed per wave** (below). Rebase the next branch after each merge; re-run CI.
6. **Subagents never:** merge, push to `main`, `terraform apply`, `helm upgrade` against Azure, change
   `docs/contracts/**` (except T-003/T-004), or edit CLAUDE.md.
7. **Shared files are orchestrator-only after T-001:** `pyproject.toml`, `uv.lock`, `Makefile`,
   `tests/conftest.py`, `docs/finops/cost-model.md`. T-001 installs the full ADR-0007 dependency set so
   lanes never need to touch them. A lane that needs a change reports it; the orchestrator applies it
   on `main` between merges and the lane rebases. Azure-resource tasks write `docs/finops/deltas/T-xxx.md`;
   the orchestrator folds deltas into the cost model at the end of each wave.
8. **The main session implements only T-001** (the scaffold everything needs) and orchestrator-only
   shared-file edits; all other tasks go to subagents.

## 2. Wave plan

```
W0a  T-001 ─────────────────────────────────────────────── (sequential, main session)
W0b  L1 architect: T-003 → T-005      L2 api: T-004      L3 platform: T-002 → T-007
W0c  L1 architect: T-006              L2 infra: T-008    (≤ 3 implementing agents at once)
     ── G0: contracts-v1 ──
W1   A data:  T-101 → {T-102 → T-103, T-104} ; T-106 (parallel) → T-107 → T-105
     B infra: T-120 → {T-121, T-122, T-123, T-124, T-125} → T-126 ; T-127
     C api:   T-113 → T-110 → T-111       (+ T-112 platform, + T-501 finops — both independent)
     ── G1 ──
W2   A bench: T-201 → {T-202, T-203} → T-204   (T-301 engine can start here, pure code)
     B tier:  T-205 → T-206 ; T-207 ; T-208 (T-208 can start at W2 day 1)
     C api:   waits for T-208 → T-209 → T-210
     ── G2 ──
W3   A bt:    T-301 (if not done) → {T-302, T-303} → T-304
     B aks:   T-305 → T-306 → T-307 → T-308
     C paas:  T-309, T-310 (parallel) ; then T-311, T-312, T-313
     ── G3 ──
W4   L1 infra: T-401 → T-403 → T-404    L2 api: T-402 (after T-401)    L3 platform: T-405
     review: T-406 (security-reviewer, after all)
     ── G4 ──
W5   L1 finops: T-502 → {T-503, T-504} → T-507     L2 obs: T-505 → T-506
     ── G5 ──
W6   docs-writer: T-601 → T-602/T-603 → T-605 ; T-604 reviewers
     ── G6 ──
```

Cross-lane dependency inside a wave (e.g. T-209 needs T-208): the dependent lane works on fakes or
another task until the dependency merges; never imports from an unmerged branch.

## 3. Merge order per wave
- W0b: L1 (contracts) → L2 (OpenAPI) → L3 (CI). W0c: any order.
- W1: B (infra, no code deps) → A (data) → C (api).
- W2: B (T-208 reader first) → C → A (benchmark last, it only reads).
- W3: A (engine/worker) → B (images/helm) → C (deploy pipelines last).
- W4: L1 → L2 → L3 → review.

## 4. Orchestrator loop (what the main session does)

```
1. /wave <n>               → list ready tasks (deps done, owns disjoint), propose ≤ 3 parallel
2. For each lane: spawn the lane's agent in its own worktree with the "task brief" (§5)
3. When an agent reports done: spawn code-reviewer (+ security-reviewer if infra/auth/secrets)
4. Fix loop: send blocking findings back to the same agent (SendMessage), max 2 rounds, then escalate
5. Human (Müge) opens PR from the branch, reads review, merges in the §3 order
6. Update TASKS.md status; at end of wave run /gate <n>
```

## 5. Task brief template (orchestrator → subagent)

```
Task: T-xxx — <title>
Branch: feat/T-xxx-<slug>   Worktree: ../tidal-<lane>
Read first: CLAUDE.md, docs/specs/<SPEC>.md §…, docs/contracts/<…>.md, business-knowledge §… (if data semantics)
Owns (only these files): …
Depends on (already merged): …
Done when: <copy from TASKS.md>
Constraints: no new deps without ADR; no Azure apply; tests required; keep PR < 400 lines.
Report back: summary, files changed, test output tail, open questions, cost impact (if any).
```

## 6. Conflict & failure handling
- Ownership violation detected in diff → reviewer marks `blocking:`; changes moved to a follow-up task.
- Contract gap discovered → agent stops, writes `docs/contracts/CHANGE-REQUESTS.md` entry; architect
  decides (ADR if breaking) before work resumes.
- Flaky test → quarantine marker `@pytest.mark.flaky_quarantine` + issue; never silently retry.
- Agent stuck > 2 fix rounds → orchestrator splits the task.
