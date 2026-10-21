# Engineering Practices

## Branching & commits
- Trunk-based (ADR-0005). Branch names `type/T-xxx-slug`. Conventional Commits:
  `feat(storage): epoch parquet writer (T-106)`.
- Squash merge only; PR title becomes the commit message.

## Pull requests
- One task per PR, < ~400 changed lines excluding tests/generated.
- Template must be complete: task, what/why, evidence, cost impact, security impact.
- Required: CI green + `code-reviewer` review + human approval. `security-reviewer` for infra/auth/CI/Docker.
- Review comment tags: `blocking:` must be fixed · `suggestion:` author decides · `nit:` optional ·
  `question:` needs a reply. Every thread is resolved or answered before merge.

## Design
- Contracts-first; ADR for any decision a future reader would ask "why?" about.
- Hexagonal-ish: domain logic pure; Azure clients injected; fakes for tests.
- Patterns used deliberately (name them in code docstrings when used): Repository (sql_repo),
  Strategy (backtest strategies), Factory (create_app), Outbox-like manifest (tiering), Idempotent consumer (worker).

## Testing
- Test pyramid in SPEC-08 §4. Coverage gate 85 %. Property tests for data transforms.
- A bug fix starts with a failing test.

## Code quality
- ruff (lint + format), mypy strict, pre-commit, no TODO without an issue link.
- Docstrings on public functions; type hints everywhere.

## Working with stakeholders (communication)
- Specs and the cost report are written for a trading audience first, engineering second.
- Disagreements are resolved in ADRs with options and numbers, not in PR threads.
- Weekly short status note (`docs/status/YYYY-WW.md`): done, next, risks, cost to date.
