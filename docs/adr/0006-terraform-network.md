# ADR-0006: Terraform over Bicep; Service Bus without private endpoint
- Status: Proposed · Related: SPEC-06 §4, SPEC-07 §1
## Context
IaC must cover Azure resources and Entra ID app registrations. Private endpoints for Service Bus require Premium.
## Options
1. Bicep (Azure-native; Entra app registrations need extra tooling).
2. Terraform azurerm + azuread (one tool for both, widely used in enterprises).
## Decision
Terraform. Service Bus stays public with RBAC-only access and TLS 1.2 to avoid Premium cost.
## Consequences
State management needed (bootstrap). Document how Premium + PE would be done in production.
