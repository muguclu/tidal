# ADR-0002: One codebase, four compute targets
- Status: Proposed · Related: SPEC-07 §3
## Context
The role requires App Service, AKS, VMs and Container Apps for "diverse workloads incl. legacy and serverless".
## Options
1. Everything on AKS (simplest ops, but hides the other skills and costs more idle).
2. Match each workload to the cheapest fitting host: API→App Service, bursty query + jobs→Container Apps,
   batch fan-out→AKS spot + KEDA, legacy exporter→scheduled VM.
## Decision
Option 2 — each choice is justified by workload shape and cost, which is the interview story.
## Consequences
More IaC and pipelines to maintain; mitigated by one image with multiple entrypoints.
In a real enterprise, consolidation onto fewer platforms may be preferable (note in report).
