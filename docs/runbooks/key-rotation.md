# Runbook: key rotation and suspected leaks (T-602)

Purpose: rotate the few credentials Tidal has, and respond if one leaks. The design keeps this list short on purpose
(`docs/specs/SPEC-06-security.md`, `docs/adr/0008-threat-model.md`).

Steps that need a deployed environment are marked **[deployed]**. Terraform is applied by a human, never by an agent.

## What exists (and what does not)
| Item | Type | Where | Rotation |
|---|---|---|---|
| `cursor-hmac-key` | the only signing secret: 32 random bytes, base64, signs API pagination cursors | Key Vault `kv-tidal-dev-<suffix>` | section 1 |
| `appinsights-connection-string` | Application Insights connection string. Ingestion is Entra-only (T-506), so it is an address, not a credential | same vault | automatic, section 2 |
| GitHub OIDC federated credentials on `sp-tidal-ci`, `sp-tidal-ci-plan`, `sp-tidal-rbac-drift`, `sp-tidal-deploy` | no secret; GitHub mints a token per run | Entra, created by `infra/terraform/bootstrap` and `envs/dev/rbac.tf` | section 3 |
| Workload identities (`id-*`, KEDA, AKS federated credentials) | Managed Identity, no secret | Entra | nothing to rotate |
| Azure SQL | Entra-only (`azuread_authentication_only = true`), no SQL login or password | | nothing to rotate |
| Storage | shared key off (`shared_access_key_enabled = false`), no SAS, no local users | | nothing to rotate |
| Service Bus | SAS off (`local_auth_enabled = false`) | | nothing to rotate |

There is no client secret, no storage key, no SQL password and no Service Bus key in the repo, config or CI. The only
literal credential-shaped strings are public emulator values (Azurite `devstoreaccount1`), allow-listed in
`.gitleaks.toml`. If you find another secret anywhere, treat it as a leak (section 4).

## 1. Rotate `cursor-hmac-key` **[deployed]**
Effect: cursors issued before the change stop verifying. A client in the middle of paging gets an error and restarts
from page one. No data is touched. The API caches the key at start, so instances must restart.

Prerequisites: Terraform access to `infra/terraform/envs/dev` (remote state, see `docs/runbooks/bootstrap.md`).

1. In `infra/terraform/envs/dev` set `cursor_hmac_version` to the current value plus one (variable in `rbac.tf`; default 1).
   Pass it with `-var cursor_hmac_version=2` or in your tfvars file.
2. Review the plan. It must show only `azapi_resource.cursor_hmac` changing (the value is write-only and shown as
   sensitive; it is never stored in state):
   ```bash
   terraform plan -var cursor_hmac_version=2
   terraform apply -var cursor_hmac_version=2
   ```
3. Restart the API so it reads the new version: restart the App Service `app-tidal-api-dev-<suffix>`
   (`az webapp restart -g <rg-name> -n <webapp-name>`) and re-run the deploy workflow if the Container App `ca-tidal-query` also
   holds the key (`docs/runbooks/deploy.md`).
4. Keep `cursor_hmac_version` in your tfvars at the new number. Any change of the number, up or down, writes a new
   random key; if the variable falls back to its default of 1, that is a rotation too.

Verification: `az keyvault secret show --vault-name kv-tidal-dev-<suffix> -n cursor-hmac-key --query attributes.updated`
shows a new time (needs `Key Vault Secrets User` on the secret; do not print `.value`). Then call a paged endpoint
and use the returned cursor on page two.

Overlap: the API accepts an optional `cursor-hmac-key-previous` secret for verification only
(`TIDAL_CURSOR__SECRET_NAME`, `src/tidal/api/auth.py`). Terraform does not create it today and the API identity is granted
read on the individual secrets only, so the overlap is not wired. Do not create it by hand (principle: everything is IaC).
Accept the one-time cursor break instead; cursors are short-lived.

Rollback: apply again with the next higher version. There is no way to restore the old value, because it was never
stored. If the API cannot start after the change, check that the secret exists and that the API identity has
`Key Vault Secrets User` on it (`envs/dev/rbac.tf`).

## 2. `appinsights-connection-string`
Terraform rewrites it when the Application Insights resource changes (the version is a hash of the value). Nothing to do.
The apps do not read it yet (`apps.tf` leaves `TIDAL_OTEL__CONNECTION_STRING` unwired), so a rewrite breaks nothing.

## 3. OIDC federated credentials (CI and deploy)
There is nothing to rotate on a schedule: no secret exists. Revoke or replace when a repository, branch policy or
environment changes, or after an incident.

Subjects (one per app, see `docs/runbooks/deploy.md` and `docs/runbooks/images.md`):
| App | Subject |
|---|---|
| `sp-tidal-ci` | `repo:<owner>/<repo>:environment:dev` |
| `sp-tidal-deploy` | `repo:<owner>/<repo>:environment:dev-deploy` |
| `sp-tidal-ci-plan` | `repo:<owner>/<repo>:pull_request` and `repo:<owner>/<repo>:ref:refs/heads/main` |
| `sp-tidal-rbac-drift` | `repo:<owner>/<repo>:environment:rbac-drift` (restrict this environment to `main`) |

**Revoke now (incident) [deployed]** - removing the credential stops new tokens at once. Already issued tokens live
for their short lifetime (about an hour at most):
```bash
az ad app federated-credential list --id <app-client-id> --query "[].{name:name,subject:subject}"
az ad app federated-credential delete --id <app-client-id> --federated-credential-id <credential-id>
```
`<app-client-id>`: output `deploy_client_id` for `sp-tidal-deploy`; the others are in the bootstrap outputs
(`infra/terraform/bootstrap/outputs.tf`). Then also restrict the GitHub environment (required reviewers, branch policy).

**Replace (planned)**: change `github_repo_path` or `deploy_github_environment` (envs/dev) or the bootstrap repo variable,
run `terraform plan`, review that the credential is replaced and the roles are unchanged, then apply. A manual delete
above shows up as drift; the next apply recreates the credential, so apply only once the cause is fixed.

Verification: re-run a workflow that logs in with the app. It should fail while the credential is gone, and succeed
after the apply. Run `rbac-drift` (nightly) and check `docs/rbac/expected.json` still matches.

## 4. Suspected leak (any secret, token or key in git, logs or a chat)
Assume it is compromised. Order matters: revoke first, clean up after.

1. **Revoke or rotate** the credential. Use the table in "What exists". For a value not in that table, find what it
   grants and remove that access (Entra app, role assignment) before anything else.
2. **Find the exposure.** Locally and in CI the scanner is gitleaks (`.pre-commit-config.yaml`, `ci.yml` job
   `secrets-scan`, config `.gitleaks.toml`):
   ```bash
   uv run pre-commit run gitleaks --all-files
   gitleaks detect --source . --log-opts="--all" --redact
   ```
   The second line needs a local gitleaks install and scans all history.
3. **Check use** **[deployed]**. Look for use between exposure and revocation: Entra sign-in logs for the app, and for
   storage the alert `alert-tidal-blob-audit-<env>` and the activity-log alerts (`storage-listkeys`, `role-write`,
   `storage-write`) from `infra/terraform/modules/monitoring`. A `listKeys` event should never happen (shared key is off).
4. **Remove from git.** Deleting the file is not enough: the value stays in history and in forks. Rewriting history needs
   a human decision and a force-push to a protected branch, so ask the repository owner. Revocation in step 1 is the real fix.
5. **Record** the cause and the control that should have stopped it (a new gitleaks rule, a pre-commit hook) in an issue
   or an ADR.

A gitleaks hit on an emulator value: add an inline `# gitleaks:allow` or a narrow rule to `.gitleaks.toml` only if
the value is public and documented (Azurite). Never allow-list a real-looking value to make CI green.

## Related
- `docs/runbooks/bootstrap.md` (creates the CI apps), `docs/runbooks/deploy.md`, `docs/runbooks/images.md`
- `docs/adr/0008-threat-model.md`
