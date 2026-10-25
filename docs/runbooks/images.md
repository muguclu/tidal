# Runbook: image pipeline (`images.yml`, T-308)

One image (`tidal`, Dockerfile target `prod`), many entrypoints. Workflow: `.github/workflows/images.yml`.

## What runs when

| Trigger | Build | SBOM | Trivy vuln + secret | Push to ACR |
|---|---|---|---|---|
| PR touching `deploy/docker/**`, `src/**`, `sql/**`, `pyproject.toml`, `uv.lock` | yes | yes | yes | no (no login, read-only token) |
| tag `vX.Y.Z` | yes | yes | yes | yes, tags `sha-<12 hex>` and `vX.Y.Z` |
| `workflow_dispatch` | yes | yes | yes | yes, tag `sha-<12 hex>` |

The pushed image is the tarball that was scanned (artifact `tidal-image`, 1 day), not a rebuild.
Gate: trivy HIGH/CRITICAL with `ignore-unfixed` fails the job; the secret scan fails on any finding.
SBOM: SPDX JSON, artifact `tidal-sbom-spdx` (90 days). Pushed digest: job summary of `push-acr`.

## One-time setup (repo admin, mandatory)

The GitHub Terraform provider is not in the stack (ADR-0007), so these are manual. Checklist:

- [ ] Repository secrets (public repo: `vars.*` print unmasked in logs, ADR-0008 R-29): `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
  `AZURE_SUBSCRIPTION_ID`. Repository variable: `ACR_NAME` (registry name without `.azurecr.io`, from the Terraform output `ACR_LOGIN_SERVER`).
- [ ] Optional variable `IMAGE_ATTEST=true`: publishes build provenance and, for tag builds, an SBOM attestation
  (needs attestation support on the plan). With it off, the `attestations: write` permission is unused.
- [ ] Environment `dev` -> Deployment branches and tags: allow only `main` and tags matching `v*`.
  A `workflow_dispatch` from any other branch is expected to fail at the environment gate.
- [ ] Tag ruleset on `v*`: restrict who can create/update tags, block force-push and deletion.
- [ ] Optional: required reviewer on environment `dev` for tag releases.
- [ ] Federated credential for subject `environment:dev` and the **AcrPush** role on the ACR
  (infra change, not done by this workflow). ACR admin user stays disabled.
- [ ] Runners: GitHub-hosted only (`ubuntu-24.04`). Self-hosted runners are not allowed for this workflow.

## Integrity checks in the pipeline

The image ID is recorded after build, re-checked before `docker save` and after `docker load` in the push job;
after push the ACR digest must equal the local RepoDigest. Tag builds use `no-cache`; PRs never write the cache.
The last step always runs `docker logout` and `az logout`.

## Operations

- Release: `git tag v0.1.0 && git push origin v0.1.0`, then read the digest in the run summary.
- Deploy by digest or tag: set `apps_image_tag` (Terraform) or the Helm `image.tag`.
- Vulnerability fix path: bump the base digest in `deploy/docker/Dockerfile` or the locked dependency, re-run.
  Do not add blanket `.trivyignore` entries; any exception needs an expiry date and an issue link.
- Verify an attestation (if enabled): `gh attestation verify oci://<acr>.azurecr.io/tidal@<digest> -R <owner>/<repo>`.

## SPEC-06 section 5 supply-chain checklist (T-405)

| Item | Enforced by |
|---|---|
| Multi-stage build, non-root UID 10001, read-only root FS | `deploy/docker/Dockerfile` (T-305); chart `securityContext` (`readOnlyRootFilesystem`, `runAsNonRoot`); `chart.yml` job `chart-lint` (helm lint, kubeconform strict, kube-linter; not a required check); `tests/unit/deploy/test_helm_chart.py` asserts no privileged/host*, non-root, read-only FS, drop ALL |
| Pinned base digest | Dockerfile `FROM ...@sha256:`; Dependabot `docker` ecosystem raises bumps weekly |
| Pinned emulator and test images | Same `tag@sha256` in `docker-compose.yml`, `integration.yml`, `bench.yml`, `tests/integration/**` (azurite 3.37.0, mssql 2022-CU27-ubuntu-22.04, servicebus-emulator 2.0.1); keep them in sync when bumping |
| Helm image pinned by digest | `values-dev.yaml` sets `image.digest`; `deploy.yml` passes `--set-string image.digest`; `tests/unit/deploy/test_helm_chart.py::test_non_local_values_pin_image_by_digest` |
| `trivy image` HIGH/CRITICAL with fix | `images.yml` (required on image PRs) |
| Trivy misconfig (Dockerfile + chart) | `ci.yml` job `trivy-config` (not a required check) |
| Python dependency vulns | `ci.yml` job `dependency-audit`: `uvx pip-audit==2.9.0` on `uv export` of `uv.lock` (not required; no new runtime dependency) |
| SBOM (SPDX) | `images.yml`, artifact `tidal-sbom-spdx` |
| Action SHAs pinned, updates | all `uses:` pinned to commit SHA; Dependabot `github-actions` |
| Secrets in repo | `ci.yml` `secrets-scan` (gitleaks, `.gitleaks.toml`) |
| ACR Basic, admin user disabled, MI pulls | Terraform ACR module (infra); see One-time setup above |

