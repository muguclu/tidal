# ADR-0012: `TIDAL_ENV` is required (fail closed)

- Status: Accepted
- Date: 2026-10-22
- Deciders: Müge Güçlü (+ orchestrator, from the T-112 security review)
- Related: config-contract v1.1, T-005, T-112, T-114

## Context
`Settings.env` defaulted to `local`. A deployment that forgot `TIDAL_ENV` would silently get local rules
(connection strings, SQL password and clock override allowed). No Azure app settings exist yet, so the
default can still be removed without breaking anything deployed.

## Options
1. **Keep the `local` default** — convenient for laptops; fails open in Azure. € impact: none.
2. **Default to `prod`** — fails closed, but local runs need the variable anyway and `prod` rules would
   reject local emulators with a confusing error. € impact: none.
3. **No default (required)** — every process states its environment; a missing value stops startup with an
   error naming the variable. € impact: none.

## Decision
Option 3: `TIDAL_ENV` is required; compose, `.env.example`, tests and scripts set it explicitly.

## Consequences
Every deployment manifest (Terraform app settings, Helm values, Container Apps env) must set `TIDAL_ENV`
(dev|prod) — added to the acceptance of T-305/T-307/T-309/T-310. Removing a default is technically breaking;
treated as config-contract v1.1 because nothing consumes the old default yet.
