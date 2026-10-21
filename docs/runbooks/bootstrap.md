# Runbook: Terraform bootstrap and GitHub OIDC (T-008)

## Purpose
Create, once, the pieces every other piece of infrastructure depends on:
- resource group `rg-tidal-tfstate`, state storage account (shared key off, TLS 1.2, no public blob, versioning, 7 d soft delete) and container `tfstate`
- Entra application and service principal `sp-tidal-ci` with GitHub OIDC federated credentials (no client secret)
- role: Storage Blob Data Contributor for `sp-tidal-ci` on the `tfstate` container only

This is **human-only**. Bootstrap uses **local state** (it creates the remote state account, so it cannot use it).
Agents and CI never run `terraform apply`.

Roles granted later (not part of T-008): AcrPush, and Contributor on `rg-tidal-dev` (SPEC-06 section 2) are added by the
env/identity tasks. The `pull_request` credential must only ever receive read-only roles. `terraform apply` for envs
needs role-assignment and Graph rights and is not delegated to CI.

## Prerequisites
- Azure subscription (pay-as-you-go, see `docs/LOCAL-DEV.md` section 2) and Azure CLI, Terraform >= 1.9
- The applying user has, on the subscription: **Owner** (or Contributor + User Access Administrator), and in Entra the right to create app registrations (Application Administrator or Cloud Application Administrator, or the tenant default allowing users to register apps)
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
# review: expect 8 resources to add (RG, storage account, container, app, SP, 2 federated credentials, role assignment), 0 to change/destroy
terraform apply bootstrap.tfplan                 # HUMAN ONLY
terraform output backend_config                  # paste into infra/terraform/envs/dev/backend.tf
```
`terraform.tfstate` and `*.auto.tfvars` stay local. Keep the state file safe (it holds no secrets, but it is the only record
of these resources); copying it to a private location is recommended. `*.tfstate` is git-ignored.

### Set the GitHub variables (variables, not secrets)
```bash
cd infra/terraform/bootstrap
gh variable set AZURE_CLIENT_ID       --repo muguclu/tidal --body "$(terraform output -raw ci_client_id)"
gh variable set AZURE_TENANT_ID       --repo muguclu/tidal --body "$(terraform output -raw tenant_id)"
gh variable set AZURE_SUBSCRIPTION_ID --repo muguclu/tidal --body "$(terraform output -raw subscription_id)"
gh api -X PUT repos/muguclu/tidal/environments/dev      # create the dev environment if missing
```
The OIDC subject for the main credential is `repo:muguclu/tidal:environment:dev`, so the environment name must be exactly `dev`.

## Verification
1. `az storage account show -n <name> --query "{key:allowSharedKeyAccess,tls:minimumTlsVersion,blob:allowBlobPublicAccess}"` returns `false`, `TLS1_2`, `false`.
2. `az storage account blob-service-properties show --account-name <name> -g rg-tidal-tfstate --query "{v:isVersioningEnabled,d:deleteRetentionPolicy}"` shows versioning on and 7 days.
3. `az ad app federated-credential list --id <client id>` shows two credentials (environment dev, pull_request).
4. Run the smoke workflow: `gh workflow run oidc-smoke.yml --repo muguclu/tidal`, then `gh run watch`. It must pass and print the subscription.
5. Re-run `terraform plan` in `bootstrap/`: no changes.

## Rollback
- Before anything else uses the state account: `terraform destroy` in `bootstrap/` (same local state). The `tfstate` container must be empty; soft-deleted blobs expire after 7 days.
- Once env states exist, do not destroy. To retire only the CI identity: `terraform destroy -target=azurerm_role_assignment.ci_state_blob -target=azuread_application.ci` (this also removes the service principal and credentials).
- If the state file is lost, import resources (`terraform import`) rather than recreating; the storage account name is globally unique.

## NEEDS-CLOUD checklist (T-008)
- [ ] Subscription created, budget alert set (LOCAL-DEV day 1)
- [ ] `terraform apply` of bootstrap done by a human; `terraform plan` shows no drift
- [ ] State account hardening verified (steps 1-2)
- [ ] GitHub variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` set; environment `dev` exists
- [ ] `oidc-smoke` workflow green (`az account show` succeeds)
- [ ] `backend_config` output handed to the T-12x env task
- [ ] Orchestrator marks T-008 done in `docs/tasks/TASKS.md`
