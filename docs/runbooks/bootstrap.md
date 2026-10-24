# Runbook: Terraform bootstrap and GitHub OIDC (T-008)

## Purpose
Create, once, the pieces every other piece of infrastructure depends on:
- resource group `rg-tidal-tfstate`, state storage account (shared key off, TLS 1.2, no public blob, versioning, 7 d soft delete) and container `tfstate`
- Entra application and service principal `sp-tidal-ci` with one GitHub OIDC federated credential, subject `repo:muguclu/tidal:environment:dev` (no client secret)
- no role assignment for `sp-tidal-ci` (it never touches state)
- a `CanNotDelete` lock and `prevent_destroy` on the state account, and a lifecycle rule deleting noncurrent blob versions after 30 days

This is **human-only**. Bootstrap uses **local state** (it creates the remote state account, so it cannot use it).
Agents and CI never run `terraform apply`.

Roles granted later (not part of T-008): AcrPush on the registry only, assigned by `envs/dev/rbac.tf` (T-403). The RG-wide Contributor of the first SPEC draft was dropped; deploys use the separate app `sp-tidal-deploy` with its own GitHub environment. There is deliberately **no `pull_request` credential**: a PR token would carry every role of `sp-tidal-ci` and bypass the `environment: dev` protection. T-127 will add a separate app `sp-tidal-ci-plan` with only Reader + Storage Blob Data Reader for PR plans. `terraform apply` for envs
needs role-assignment and Graph rights and is not delegated to CI.

## Prerequisites
- Azure subscription (pay-as-you-go, see `docs/LOCAL-DEV.md` section 2) and Azure CLI, Terraform >= 1.9
- The applying user has, on the subscription: **Owner** (or Contributor + User Access Administrator), and in Entra the right to create app registrations (Application Administrator or Cloud Application Administrator, or the tenant default allowing users to register apps)
- Because shared keys are off and the provider uses `storage_use_azuread = true`, the applier also needs a data-plane role on the state account for later `terraform init` with the azurerm backend. Owner does not include data access. After the first apply grant yourself: `az role assignment create --assignee <your upn> --role "Storage Blob Data Contributor" --scope <container id from terraform output>` (not managed by Terraform on purpose, it is a personal grant).
- GitHub: admin on `muguclu/tidal`, `gh` CLI authenticated, a repository environment named `dev`
- A globally unique state account name, e.g. `sttidaltfstate` + 4 random lowercase chars

## Steps
```bash
az login
az account set --subscription <SUBSCRIPTION_ID>
az account show --query "{sub:id,tenant:tenantId}"

cd infra/terraform/bootstrap
cp examples/dev.tfvars.example dev.auto.tfvars   # edit: subscription_id, state_account_name, owner
terraform init
terraform plan -out bootstrap.tfplan
# review: expect 8 resources to add (RG, storage account, lock, lifecycle policy, container, app, SP, 1 federated credential) plus the data sources, 0 to change/destroy
terraform apply bootstrap.tfplan                 # HUMAN ONLY
terraform output backend_config                  # paste into infra/terraform/envs/dev/backend.tf
```
`terraform.tfstate` and `*.auto.tfvars` stay local. Keep the state file safe (it holds no secrets, but it is the only record
of these resources); copying it to a private location is recommended. `*.tfstate` is git-ignored.

### Create the protected `dev` environment
Required reviewer `muguclu` and deployment branches restricted to `main` (the user id is looked up first):
```bash
UID_GH=$(gh api users/muguclu --jq .id)
gh api -X PUT repos/muguclu/tidal/environments/dev --input - <<EOF
{
  "reviewers": [{"type": "User", "id": $UID_GH}],
  "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}
}
EOF
gh api -X POST repos/muguclu/tidal/environments/dev/deployment-branch-policies -f name=main -f type=branch
```
Verify both settings:
```bash
gh api repos/muguclu/tidal/environments/dev --jq '{reviewers: [.protection_rules[]|select(.type=="required_reviewer")|.reviewers[].reviewer.login], custom_branches: .deployment_branch_policy.custom_branch_policies}'
gh api repos/muguclu/tidal/environments/dev/deployment-branch-policies --jq '[.branch_policies[].name]'
# expect reviewers ["muguclu"], custom_branches true, branches ["main"]
```
Each smoke run then waits for approval, and runs dispatched from other branches are rejected.

### Set the GitHub variables (variables, not secrets)
```bash
cd infra/terraform/bootstrap
gh variable set AZURE_CLIENT_ID       --repo muguclu/tidal --body "$(terraform output -raw ci_client_id)"
gh variable set AZURE_TENANT_ID       --repo muguclu/tidal --body "$(terraform output -raw tenant_id)"
gh variable set AZURE_SUBSCRIPTION_ID --repo muguclu/tidal --body "$(terraform output -raw subscription_id)"
```
The OIDC subject for the main credential is `repo:muguclu/tidal:environment:dev`, so the environment name must be exactly `dev`.

## Verification
1. `az storage account show -n <name> --query "{key:allowSharedKeyAccess,tls:minimumTlsVersion,blob:allowBlobPublicAccess}"` returns `false`, `TLS1_2`, `false`.
2. `az storage account blob-service-properties show --account-name <name> -g rg-tidal-tfstate --query "{v:isVersioningEnabled,d:deleteRetentionPolicy}"` shows versioning on and 7 days.
3. `az ad app federated-credential list --id <client id>` shows exactly one credential (subject `repo:muguclu/tidal:environment:dev`).
4. Run the smoke workflow: `gh workflow run oidc-smoke.yml --repo muguclu/tidal`, then `gh run watch`. It must pass. The workflow logs in with `allow-no-subscriptions: true` because `sp-tidal-ci` has no subscription or resource-group role until `envs/dev` is applied (T-403 grants AcrPush on the registry only); until then `az account show` may print only the tenant, or the subscription may not be listed. That still proves OIDC works.
5. Re-run `terraform plan` in `bootstrap/`: no changes.

## Rollback
The state account is protected by `prevent_destroy` and a `CanNotDelete` lock, so removal is deliberate:
1. Make sure no env state is left (`az storage blob list --account-name <name> -c tfstate --auth-mode login`).
2. Remove the lock first: `terraform apply -target=azurerm_management_lock.tfstate_account -destroy` (or `az lock delete --name lock-tfstate-cannotdelete --resource-group rg-tidal-tfstate --resource-name <name> --resource-type Microsoft.Storage/storageAccounts`, then `terraform state rm azurerm_management_lock.tfstate_account`).
3. Temporarily remove the `lifecycle { prevent_destroy = true }` blocks from the account and container in `main.tf` (do not commit this).
4. `terraform destroy` in `bootstrap/` (same local state). Soft-deleted blobs expire after 7 days.
- To retire only the CI identity: `terraform destroy -target=azuread_application.ci` (also removes the service principal and credential); no lock change needed.
- If the state file is lost, import resources (`terraform import`) rather than recreating; the storage account name is globally unique.

## NEEDS-CLOUD checklist (T-008)
- [ ] Subscription created, budget alert set (LOCAL-DEV day 1)
- [ ] `terraform apply` of bootstrap done by a human; `terraform plan` shows no drift
- [ ] State account hardening verified (steps 1-2)
- [ ] `dev` environment has required reviewer and `main`-only branch policy (verification calls above)
- [ ] GitHub variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` set; environment `dev` exists
- [ ] `oidc-smoke` workflow green (`az account show` succeeds)
- [ ] `backend_config` output handed to the T-12x env task
- [ ] Orchestrator marks T-008 done in `docs/tasks/TASKS.md`
