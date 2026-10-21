# ADR-0006: Terraform over Bicep; Service Bus without private endpoint

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ architect agent)
- Related: [SPEC-06 §2, §4](../specs/SPEC-06-security.md), [SPEC-07 §1](../specs/SPEC-07-infra-platform.md),
  [SPEC-08 §2](../specs/SPEC-08-cicd-engineering.md), [plan review #10](../reviews/2026-10-21-plan-review.md),
  ADR-0003, T-008, T-126, T-404, T-501

## Context
Everything in Azure must exist as code (CLAUDE.md §2.4), including Entra ID app registrations and app
roles. The role asks for Azure infrastructure, **Entra ID / RBAC / Key Vault** and cost awareness. In
Phase 4 Blob, SQL and Key Vault get private endpoints, but a Service Bus private endpoint requires the
Premium tier.

## Options
Infrastructure-as-code tool:
1. **Bicep.** Pros: Azure-native, no state file. Cons: Entra app registrations need extra tooling (the
   Microsoft Graph Bicep extension). € impact: none.
2. **Terraform (azurerm + azuread).** Pros: one tool for Azure resources and Entra objects; widely used in
   enterprises; `plan` output reviewable on PRs. Cons: remote state must be bootstrapped and protected.
   € impact: state storage account cost to be measured in T-502.

Service Bus network:
3. **Premium + private endpoint.** Pros: no public endpoint. Cons: highest tier for three small queues.
   € impact: to be priced in T-501 (`prices.yaml`).
4. **Basic, public endpoint, Entra RBAC only, TLS 1.2 minimum.** Pros: cheapest. Cons: endpoint reachable
   from the internet (still requires an Entra token with a data role). € impact: < €1/month (cost-model).

## Decision
Terraform (option 2) with Service Bus public behind RBAC and TLS 1.2 (option 4), because one tool covers
Azure and Entra, and Premium would cost more than the queues are worth in a dev environment.

## Consequences
- Bootstrap (T-008) creates the state account once with local state; versioning and soft delete on.
- `terraform apply` is **human-only**. `iac.yml` runs fmt/validate/tflint/checkov/plan; `deploy.yml`
  **never runs `terraform apply`**, it only deploys images. The CI identity has AcrPush and Contributor on
  `rg-tidal-dev`, which is not enough for role assignments or Graph changes.
- Service Bus is reached only through managed identities with data roles (SPEC-06 §2).
- The final report documents how Premium + private endpoint would be done in production.
- Monitor: checkov findings, plan drift, `az role assignment list` diff (T-403).
- Revisit if the environment carries real data or a compliance rule forbids public endpoints.
