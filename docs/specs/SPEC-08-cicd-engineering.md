# SPEC-08 — CI/CD & Engineering Practices

Owner agents: `platform-engineer` (pipelines), `test-engineer` (gates), `code-reviewer` (process)
Tasks: T-001 … T-008, T-311, T-312 · See also `docs/engineering-practices.md`

## 1. Branching strategy: trunk-based with short-lived branches
- `main` is always deployable; protected: PR required, 1 approving review (CODEOWNERS), status checks
  required, linear history (squash merge), signed commits, no force push, conversation resolution required.
- Branches: `feat|fix|chore|docs|infra/T-xxx-slug`, lifetime < 3 days.
- Releases: tag `vX.Y.Z` on `main` (semantic-release from Conventional Commits) → deploys to `dev`
  with GitHub **environment** approval.
- Why trunk-based not GitFlow: small team, CD to one environment, fewer long-lived merge conflicts —
  recorded in ADR-0005 (with when GitFlow would be preferable).

> Solo-developer note: GitHub does not allow approving your own PR. Use a second GitHub account
> ("reviewer bot" owned by you) for CODEOWNERS approval **or** document a rule that the
> `code-reviewer` subagent's review is posted as a PR comment and required check `review-checklist`
> must pass. Be transparent about this in the README.

## 2. Workflows (`.github/workflows/`)

| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yml` | PR, push main | lint (ruff), type (mypy), unit+contract tests (pytest, coverage ≥ 85 % gate), gitleaks, openapi diff (oasdiff), schema contract tests |
| `integration.yml` | PR label `integration`, nightly | docker compose up → integration tests (Azurite, SQL Server container, SB emulator) |
| `iac.yml` | PR touching `infra/**` | fmt, validate, tflint, checkov, `terraform plan` (OIDC) → plan posted as PR comment |
| `images.yml` | push main, tags | build, trivy, syft SBOM, push to ACR (`sha-*`, `vX.Y.Z`) |
| `deploy.yml` | release tag, manual | environment `dev` (required reviewer) → terraform apply → helm upgrade → az webapp/containerapp update → smoke tests |
| `bench.yml` | manual | run storage benchmark at S scale, upload results artefact |
| `cost.yml` | weekly cron | export last 7 d cost by tag → commit `docs/finops/results/weekly-*.md` via PR |

All Azure access via `azure/login` with OIDC (`id-token: write`), no client secrets.

## 3. Quality gates (required checks on `main`)
`lint`, `types`, `tests`, `coverage`, `contracts`, `openapi-diff`, `secrets-scan`, `iac` (if infra changed),
`image-scan` (if Dockerfile changed), `review-checklist`.

## 4. Test pyramid

| Level | Location | Tooling | Runs |
|---|---|---|---|
| Unit | `tests/unit` | pytest, hypothesis | every PR |
| Contract | `tests/contract` | schema equality, schemathesis, message JSON Schema | every PR |
| Integration | `tests/integration` | testcontainers / compose | label + nightly |
| Benchmark | `benchmarks/` | pytest-benchmark | manual / weekly |
| Load | `tests/load` | locust against dev | before G3 / G5 |
| Smoke | `tests/smoke` | pytest + real tokens | after deploy |

## 5. Code review process
- PR template (`.github/pull_request_template.md`) — task ID, what/why, test evidence, cost impact,
  security impact, screenshots.
- Reviewer checklist mirrors `code-reviewer` agent; every comment tagged `blocking:`, `suggestion:`, `nit:`,
  `question:`.
- Reviews within 24 h; author resolves or replies to every thread.

## 6. Acceptance criteria
- [ ] Branch protection configured via `gh api` script `scripts/protect-main.sh` (reproducible, committed).
- [ ] A deliberately bad PR (failing test, leaked fake secret, breaking API change) is blocked by CI — keep
      it as a closed PR in history as evidence.
- [ ] Deploy pipeline completes from tag to smoke-tested env in < 15 min.
