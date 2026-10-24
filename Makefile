# Tidal — developer entrypoints (CLAUDE.md §5). Shared file: orchestrator-only after T-001.
# Targets marked "stub" fail loudly until the owning task wires them.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV  ?= uv
ENV ?= dev

define stub
	@echo "make $@: not implemented yet — owned by $(1)" >&2; exit 1
endef

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-16s %s\n", $$1, $$2}'

## ---- everyday --------------------------------------------------------------------------------
.PHONY: install
install: ## uv sync (dev group) + install git hooks
	$(UV) sync
	$(UV) run pre-commit install

.PHONY: lint
lint: ## ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## ruff autofix + format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

.PHONY: types
types: ## mypy --strict
	$(UV) run mypy

.PHONY: check
check: lint types ## lint + types (what CI's lint/types jobs run)

.PHONY: test
test: ## fast tests with coverage gate (no docker)
	$(UV) run pytest -m "not integration" --cov --cov-report=term-missing

.PHONY: test-int
test-int: ## integration tests (needs docker)
	$(UV) run pytest -m integration

.PHONY: hooks
hooks: ## run all pre-commit hooks on all files
	$(UV) run pre-commit run --all-files

## ---- contracts & api -----------------------------------------------------------------------
.PHONY: contracts
contracts: ## regenerate docs/contracts/schemas/*.json from src/tidal/contracts
	$(UV) run python -m tidal.contracts.export

.PHONY: openapi-diff
openapi-diff: ## oasdiff breaking, both directions (stub)
	$(call stub,T-110)

## ---- data & benchmarks ---------------------------------------------------------------------
.PHONY: bench
bench: ## storage benchmarks
	$(UV) run --group bench pytest benchmarks/ --benchmark-only

## ---- infra & cloud (never apply from a subagent) -------------------------------------------
.PHONY: tf-plan
tf-plan: ## terraform plan for ENV (stub)
	$(call stub,T-126)

.PHONY: park
park: ## quick park: stop AKS/App Service/jobs/VM (PARK_ARGS=--dry-run to preview; docs/runbooks/park.md)
	bash scripts/park.sh $(PARK_ARGS)

.PHONY: unpark
unpark: ## reverse of park (PARK_ARGS=--dry-run to preview)
	bash scripts/unpark.sh $(PARK_ARGS)

## ---- finops --------------------------------------------------------------------------------
.PHONY: cost-report
cost-report: ## regenerate docs/finops/results/*.md (stub)
	$(call stub,T-502)

.PHONY: clean
clean: ## remove caches and build output
	rm -rf .mypy_cache .ruff_cache .pytest_cache .hypothesis .coverage htmlcov dist build
