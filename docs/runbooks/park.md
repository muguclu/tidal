# Runbook: park and unpark dev (T-313)

Idle cost is the main FinOps lever (`docs/finops/cost-model.md`). Two levels:

| Level | How | Reversible in | Who runs it |
|---|---|---|---|
| Quick park | `scripts/park.sh` (az only) | minutes | anyone with rights on the dev resource group |
| Deep park | Terraform variable flip | one apply | a human only; the script prints the command and never runs it |

## Quick park / unpark
```bash
make park          # = scripts/park.sh
make unpark        # = scripts/unpark.sh
scripts/park.sh --dry-run   # prints the az commands, makes no az calls
```
Names come from the same variables as the deploy workflow (`docs/runbooks/deploy.md`), from the environment or from a
`deploy.env` (`KEY=VALUE`, parsed, never sourced; `--env-file FILE` to override): `TIDAL_RESOURCE_GROUP` (required),
`TIDAL_AKS_NAME`, `TIDAL_WEBAPP_NAME`, `TIDAL_JOB_NAMES`, plus `TIDAL_VM_NAME` and `TIDAL_LOGIC_APP_NAME`
(terraform outputs `legacy_vm_name`, `legacy_vm_logic_app_name`). An empty name skips that component. Optional
`AZURE_SUBSCRIPTION_ID` is passed as `--subscription`. Log in first (`az login`); the script never logs in.

Order of park: Logic App disabled (so nothing restarts the VM), VM deallocated, Container Apps jobs stopped, App
Service stopped, AKS stopped. Unpark is the exact reverse: AKS start first (`--no-wait`, slowest), then App Service,
jobs, VM, Logic App last. Every step reads the current state first and skips when it already matches, so a second
run issues no state-changing command (covered by `tests/unit/scripts/test_park.py` with a fake `az`).

Verify after unpark: `az aks show --query powerState.code` is `Running` (a few minutes), then re-run the deploy
workflow smoke tests (`docs/runbooks/deploy.md`) if you want proof.

## What still bills while quick-parked
Figures are the planning estimates in `docs/finops/cost-model.md` (EUR/month, not measurements):

| Item | EUR/month |
|---|---|
| ACR Basic | 4.40 |
| App Service B1 plan (a stopped app still bills the plan) | ~12 |
| AKS LB + public IP + OS disk | 24.38 if `az aks stop` keeps LB + IP, 5.11 if released (**unverified**) |
| VM disk | ~1 |
| Storage | ~0.04 |
| Logic App, Key Vault | < 0.1, < 0.03 |
| Azure SQL | 0 on the free offer (3.85 list, storage only) |
| Model total parked | ~45 (~33 with `enable_app_service=false`) |

## Deep park (human)
From `infra/terraform/envs/dev`, review the plan, then:
```bash
terraform apply -var park=true -var enable_app_service=false -var enable_legacy_vm=false
```
This deletes the App Service plan (~12) and the VM, disk and Logic App (~1) and sets the `park=true` tag. To revert,
apply with `park=false -var enable_app_service=true -var enable_legacy_vm=true`, then deploy.

## AKS decision (open, needs a measurement)
The cost model flags that the LB and public IP (19.27 of the 24.38) may keep billing after `az aks stop`. Before the
first park of more than a few days, run `az aks stop`, then check `az network lb list` / `az network public-ip list` in
the node resource group (`MC_*`) and Cost Management after a day. If they remain, the rule is: park under about a
week with `az aks stop`; longer, delete the cluster (there is no Terraform variable for it today, so that is a
manual `terraform destroy -target` decision by a human, or a new `enable_aks` flag via a separate infra task).
Record the result in `docs/finops/` and replace "unverified" above.

## Caveats
- Container Apps jobs: `az containerapp job stop/start` and the `properties.runningState` field are used to suspend
  and resume cron triggers. This is unverified against a live subscription; if the CLI extension (pinned `0.3.55` in
  the deploy workflow) lacks it the step fails loudly. Jobs are scale-to-zero, so the saving is small either way.
- Service Bus Basic, ACR Basic and Storage have no off switch short of deleting them.
