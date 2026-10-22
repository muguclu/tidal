# envs/dev

Composition root of the Tidal dev environment (SPEC-07 section 1, task T-126). It wires the nine merged modules
together with one tag set from `module.resource_group.tags`, and exposes the values the application needs as the
`tidal_config` output.

## What is composed

| Module | Component tag | Notes |
|---|---|---|
| `resource_group` | foundation | `rg-tidal-dev`, source of the tags for every other module |
| `log_analytics` | observability | PerGB2018, 0.2 GB/day cap, workspace-based App Insights |
| `identity` | identity | six user-assigned identities (api, query, worker, ingest, tiering, legacy-vm) |
| `storage` | storage | `sttidaldev<name_suffix>`, shared key off, containers per storage-layout contract |
| `key_vault` | secrets | `kv-tidal-dev-<name_suffix>`, RBAC, purge protection |
| `acr` | registry | Basic, admin off, weekly purge task |
| `sql` | data | serverless, Entra-only, free offer by default |
| `servicebus` | messaging | Basic, queues per messaging-contract section 1 |
| `budget` | finops | EUR 60, subscription scope, 50/80/100 % alerts |

Not composed yet (the modules do not exist): network (T-404), aks (T-306), app_service and container_apps (T-309),
vm_legacy (T-310). Role assignments (SPEC-06 section 2) arrive with those consumers and with T-401/T-403, so after
this task the identities exist but hold no roles.

## Flags

| Variable | Default | Effect |
|---|---|---|
| `enable_private_networking` | `false` | passed to storage, key_vault, sql; needs the network module (T-404) before it is useful |
| `enable_ingest_topic` | `false` | Service Bus Standard + `ingest-events` topic (extra base charge) |
| `use_free_offer` | `true` | SQL database on the Azure SQL free offer (ADR-0011) |
| `budget_scope` | `subscription` | covers the AKS `MC_*` node group |
| `park` | `false` | value of the `park` tag |
| `name_suffix` | required | one suffix for all globally unique names (3 to 8 lowercase letters/digits) |

## Output to config mapping

`terraform output -json tidal_config` (no secrets) and `tidal_config_sensitive`.

| Output key | Config contract key | Module output |
|---|---|---|
| `TIDAL_ENV` | `TIDAL_ENV` | constant `dev` |
| `TIDAL_STORAGE__ACCOUNT_URL` | `TIDAL_STORAGE__ACCOUNT_URL` | `storage.primary_blob_endpoint` (trailing slash removed) |
| `TIDAL_STORAGE__USE_MANAGED_IDENTITY` | same | constant `true` |
| `TIDAL_SQL__SERVER` | same | `sql.server_fqdn` |
| `TIDAL_SQL__DATABASE` | same | `sql.database_name` |
| `TIDAL_SQL__AUTH` | same | constant `entra_mi` |
| `TIDAL_SERVICEBUS__FQDN` | same | `servicebus.fqdn` |
| `TIDAL_AUTH__TENANT_ID` | same | `azurerm_client_config.tenant_id` |
| `ACR_LOGIN_SERVER` | outside `TIDAL_` | `acr.login_server` |
| `KEY_VAULT_URI` | outside `TIDAL_` | `key_vault.vault_uri` |
| `IDENTITY_CLIENT_ID_<API,QUERY,WORKER,INGEST,TIERING,LEGACY_VM>` | outside `TIDAL_` (`AZURE_CLIENT_ID` of the workload that runs as that identity) | `identity.identities[*].client_id` |
| `TIDAL_OTEL__CONNECTION_STRING` (`tidal_config_sensitive`, sensitive) | same | `log_analytics.app_insights_connection_string` |

`TIDAL_QUERY__BASE_URL` is added with the Container Apps module (T-309).

## NEEDS-CLOUD: human-only steps

Agents and CI never run these. CI runs `plan` read-only once `AZURE_PLAN_CLIENT_ID` is set
(`.github/workflows/iac.yml`).

0. **SQL Entra admin group** (once). SQL is Entra-only, so the server needs an admin group before the first apply:
   ```bash
   az login && az account set --subscription <SUBSCRIPTION_ID>
   az ad group create --display-name sg-tidal-sql-admins --mail-nickname sg-tidal-sql-admins
   az ad group member add --group sg-tidal-sql-admins --member-id "$(az ad signed-in-user show --query id -o tsv)"
   az ad group show --group sg-tidal-sql-admins --query id -o tsv   # -> sql_entra_admin_object_id
   ```
1. **Configure.** Prerequisites: `docs/runbooks/bootstrap.md` is done and you hold Storage Blob Data Contributor on the
   `tfstate` container (shared key is off) and Contributor (or Owner) on the subscription (the budget is
   subscription scoped).
   ```bash
   cd infra/terraform/envs/dev
   cp dev.tfvars.example dev.tfvars        # git-ignored; fill in every placeholder
   cp backend.hcl.example backend.hcl      # fill in from `terraform output backend_config` (bootstrap); do not commit
   ```
2. **Init** (Entra auth, remote state):
   ```bash
   terraform init -backend-config=backend.hcl
   ```
3. **Plan** and review. Expect only creates (no changes or destroys on a clean state):
   ```bash
   terraform plan -var-file=dev.tfvars -out dev.tfplan
   ```
   The `budget_start_date` must be the first day of the current month. If the forecast alert is rejected on a
   brand-new budget, apply again later (see the budget module README).
4. **Apply** (human only):
   ```bash
   terraform apply dev.tfplan
   ```
5. **Write the outputs to `.env.cloud`** at the repo root (git-ignored through `.env.*`; never commit it):
   ```bash
   umask 077
   {
     terraform output -json tidal_config           | jq -r 'to_entries[] | "\(.key)=\(.value)"'
     terraform output -json tidal_config_sensitive | jq -r 'to_entries[] | "\(.key)=\(.value)"'
   } > ../../../../.env.cloud
   ```
   The connection string is a secret: keep file permissions private and delete the file when the cloud window ends.
6. **Map identities in SQL** (after the first apply). Connect as a member of the admin group (Entra token) and map each
   identity by its **client id** (`identities.<name>.client_id`, also `IDENTITY_CLIENT_ID_*`). The server has no
   system-assigned identity, so it cannot resolve names through Microsoft Graph and the `OBJECT_ID` clause is mandatory.
   Public access is on without firewall rules, so open a temporary rule for your IP first and delete it afterwards:
   ```bash
   MYIP=$(curl -s https://api.ipify.org)
   az sql server firewall-rule create -g rg-tidal-dev -s <sql server name> -n tmp-operator --start-ip-address "$MYIP" --end-ip-address "$MYIP"
   # as an admin-group member, in the tidal database:
   #   CREATE USER [id-api] FROM EXTERNAL PROVIDER WITH OBJECT_ID='<client id of id-api>';
   #   then the role grants from SPEC-06 section 2
   az sql server firewall-rule delete -g rg-tidal-dev -s <sql server name> -n tmp-operator
   ```
7. **Teardown** at the end of the cloud window: `terraform destroy -var-file=dev.tfvars` removes everything except
   bootstrap. The Key Vault stays soft-deleted for 7 days (purge protection).

## Validation (no credentials needed)

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate   # variables without defaults are only needed for plan
tflint --config=.tflint.hcl
checkov -d . --framework terraform
```
`.tflint.hcl` here is required by the `iac.yml` matrix, which looks for a config in each root. Checkov skips are
inherited from the modules and carry their justifications there.

The committed `.terraform.lock.hcl` covers linux_amd64, windows_amd64 and darwin_arm64.
