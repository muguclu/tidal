# ADR-0002: One codebase, four compute targets

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ architect agent)
- Related: [SPEC-07 §2–§4](../specs/SPEC-07-infra-platform.md), [SPEC-04 §1](../specs/SPEC-04-query-api.md),
  [cost-model](../finops/cost-model.md), [plan review #12, #23](../reviews/2026-10-21-plan-review.md), T-305, T-306, T-309, T-310

## Context
The job asks for experience with **App Service, AKS, VMs and Container Apps for diverse workloads,
including legacy and serverless**. Tidal's workloads have very different shapes: an always-on API, a
bursty query service, large backtest fan-outs, and a nightly CSV feed for a downstream risk system. The
budget is ≤ €60/month active and ≤ €15/month parked (PLANNING §6).

## Options
1. **Everything on AKS.**
   - Pros: one platform to operate; one deployment path.
   - Cons: hides three of the four skills the role asks for; the cluster must run (or be unparked) for
     every API call, so idle cost does not drop to zero.
   - € impact: AKS system pool €10–35 + disks/LB/IP €3–6 per active month (cost-model); always-on AKS
     baseline vs spot + KEDA to be measured in T-504 (C4).
2. **Match each workload to the cheapest fitting host.** API → App Service B1; bursty query + cron/event
   jobs → Container Apps (min 0); backtest fan-out → AKS spot pool + KEDA (min 0); legacy exporter →
   scheduled B1s VM started by a Logic App.
   - Pros: each host is justified by workload shape and cost; shows all four skills.
   - Cons: four deployment targets, more IaC and pipelines to maintain.
   - € impact (cost-model, active month): App Service ~€12, Container Apps €0–3, AKS spot €1–5 on top of
     the system pool, VM €2–4, Logic App < €0.1. C5 (Container Apps vs always-on) measured in T-504.

## Decision
Option 2, because each host choice can be defended by workload shape and a cost number, which is
exactly the story the role asks for.

## Consequences
- (Note 2026-10-22: the `tidal-ingest` entrypoint was removed by ADR-0013; real-source ingestion no longer exists.)
- One image with five entrypoints (`tidal-api`, `tidal-query`, `tidal-worker`, `tidal-ingest`,
  `tidal-tiering`) keeps the build single; image size is measured in T-305 (target ≤ 500 MB).
- Query ingress: **external with Entra token validation** (audience `TIDAL_QUERY__AUDIENCE`) while
  `enable_private_networking=false`, because App Service cannot reach internal ingress without VNet
  integration; **internal** when the flag is on (SPEC-04 §1).
- The 0→N AKS demo needs PAYG and spot quota; the free trial cannot run it (LOCAL-DEV §2).
- Monitor: per-host cost by tag, Container Apps cold-start p95 (C5), spot evictions.
- Revisit at G3 with measured numbers. In a real enterprise, consolidating onto fewer platforms may be
  better for operations; the final report says so.
