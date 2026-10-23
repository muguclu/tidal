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

## One-time setup (repo admin)

Repository variables (not secrets): `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`ACR_NAME` (registry name without `.azurecr.io`, from the Terraform output `ACR_LOGIN_SERVER`).
Optional `IMAGE_ATTEST=true` to publish build provenance (needs attestation support on the plan).

The OIDC principal needs a federated credential for `environment:dev` and the **AcrPush** role on the ACR
(infra change, not done by this workflow). ACR admin user stays disabled.

## Operations

- Release: `git tag v0.1.0 && git push origin v0.1.0`, then read the digest in the run summary.
- Deploy by digest or tag: set `apps_image_tag` (Terraform) or the Helm `image.tag`.
- Vulnerability fix path: bump the base digest in `deploy/docker/Dockerfile` or the locked dependency, re-run.
  Do not add blanket `.trivyignore` entries; any exception needs an expiry date and an issue link.
- Verify an attestation (if enabled): `gh attestation verify oci://<acr>.azurecr.io/tidal@<digest> -R <owner>/<repo>`.
