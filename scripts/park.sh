#!/usr/bin/env bash
# park.sh - "quick park" of the dev environment (T-313). unpark.sh calls this with --unpark.
#
# Quick park is az-only and reversible in minutes. Deep park is a Terraform variable flip that
# only a human applies: this script prints the command and never runs terraform.
#
# Resource names come from the same variables as the deploy workflow (docs/runbooks/deploy.md),
# taken from the environment or from a KEY=VALUE file (--env-file, default ./deploy.env if present):
#   TIDAL_RESOURCE_GROUP  TIDAL_AKS_NAME  TIDAL_WEBAPP_NAME  TIDAL_JOB_NAMES (space separated)
#   TIDAL_VM_NAME  TIDAL_LOGIC_APP_NAME  (terraform outputs legacy_vm_name / legacy_vm_logic_app_name)
# Optional: AZURE_SUBSCRIPTION_ID is passed as --subscription when set.
# An empty/unset name skips that component (e.g. App Service deleted by enable_app_service=false).
set -euo pipefail

MODE="park"
DRY_RUN=0
ENV_FILE=""
usage() {
  echo "usage: $0 [--unpark] [--dry-run] [--env-file FILE]" >&2
  exit 2
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --unpark) MODE="unpark" ;;
    --dry-run) DRY_RUN=1 ;;
    --env-file)
      [ "$#" -ge 2 ] || usage
      ENV_FILE="$2"
      shift
      ;;
    *) usage ;;
  esac
  shift
done

# Load KEY=VALUE lines for known keys only; never source or eval the file.
if [ -z "$ENV_FILE" ] && [ -f deploy.env ]; then ENV_FILE="deploy.env"; fi
if [ -n "$ENV_FILE" ]; then
  [ -f "$ENV_FILE" ] || {
    echo "env file not found: $ENV_FILE" >&2
    exit 2
  }
  while IFS='=' read -r key value || [ -n "$key" ]; do
    case "$key" in
      TIDAL_RESOURCE_GROUP | TIDAL_AKS_NAME | TIDAL_WEBAPP_NAME | TIDAL_JOB_NAMES | TIDAL_VM_NAME | TIDAL_LOGIC_APP_NAME | AZURE_SUBSCRIPTION_ID)
        value="${value%\"}"
        value="${value#\"}"
        if [ -z "${!key:-}" ]; then export "$key=$value"; fi
        ;;
      *) ;;
    esac
  done <"$ENV_FILE"
fi

RG="${TIDAL_RESOURCE_GROUP:-}"
AKS="${TIDAL_AKS_NAME:-}"
WEBAPP="${TIDAL_WEBAPP_NAME:-}"
JOBS="${TIDAL_JOB_NAMES:-}"
VM="${TIDAL_VM_NAME:-}"
LOGIC="${TIDAL_LOGIC_APP_NAME:-}"
SUB="${AZURE_SUBSCRIPTION_ID:-}"

[ -n "$RG" ] || {
  echo "TIDAL_RESOURCE_GROUP is required" >&2
  exit 2
}
name_re='^[A-Za-z0-9][A-Za-z0-9._-]{0,89}$'
for pair in "AKS:$AKS" "WEBAPP:$WEBAPP" "VM:$VM" "LOGIC:$LOGIC" "RG:$RG"; do
  v="${pair#*:}"
  if [ -n "$v" ] && ! [[ "$v" =~ $name_re ]]; then
    echo "invalid ${pair%%:*} name: $v" >&2
    exit 2
  fi
done

SUB_ARGS=()
if [ -n "$SUB" ]; then SUB_ARGS=(--subscription "$SUB"); fi

# run_change CMD...: the only place that mutates Azure. Prints in dry-run, executes otherwise.
run_change() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] az $*"
  else
    echo "  + az $*"
    az "$@" ${SUB_ARGS[@]+"${SUB_ARGS[@]}"}
  fi
}
# query_state CMD...: read-only; prints the value lower-cased. Not called in dry-run.
query_state() {
  local out
  out="$(az "$@" ${SUB_ARGS[@]+"${SUB_ARGS[@]}"} -o tsv 2>/dev/null || true)"
  printf '%s' "$out" | tr '[:upper:]' '[:lower:]'
}

# step LABEL WANTED_REGEX QUERY_CMD... -- CHANGE_CMD...
# Skips the change when the current state already matches WANTED_REGEX.
step() {
  local label="$1" want="$2"
  shift 2
  local -a q=() c=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    q+=("$1")
    shift
  done
  shift
  c=("$@")
  echo "- $label"
  if [ "$DRY_RUN" -eq 1 ]; then
    run_change "${c[@]}"
    return 0
  fi
  local state
  state="$(query_state "${q[@]}")"
  if [[ "$state" =~ $want ]]; then
    echo "  already ${state}, skipped"
  else
    run_change "${c[@]}"
  fi
}

do_logic() {
  [ -n "$LOGIC" ] || return 0
  if [ "$MODE" = park ]; then
    step "Logic App $LOGIC -> Disabled" '^disabled$' \
      logic workflow show -g "$RG" -n "$LOGIC" --query state -- \
      logic workflow update -g "$RG" -n "$LOGIC" --state Disabled
  else
    step "Logic App $LOGIC -> Enabled" '^enabled$' \
      logic workflow show -g "$RG" -n "$LOGIC" --query state -- \
      logic workflow update -g "$RG" -n "$LOGIC" --state Enabled
  fi
}
do_vm() {
  [ -n "$VM" ] || return 0
  if [ "$MODE" = park ]; then
    step "VM $VM -> deallocated" 'deallocated' \
      vm show -d -g "$RG" -n "$VM" --query powerState -- \
      vm deallocate -g "$RG" -n "$VM"
  else
    step "VM $VM -> running" '^vm running$' \
      vm show -d -g "$RG" -n "$VM" --query powerState -- \
      vm start -g "$RG" -n "$VM"
  fi
}
do_jobs() {
  local j
  for j in $JOBS; do
    [[ "$j" =~ $name_re ]] || {
      echo "invalid job name: $j" >&2
      exit 2
    }
    if [ "$MODE" = park ]; then
      step "Container Apps job $j -> suspended" '^(suspended|stopped)$' \
        containerapp job show -g "$RG" -n "$j" --query properties.runningState -- \
        containerapp job stop -g "$RG" -n "$j"
    else
      step "Container Apps job $j -> running" '^running$' \
        containerapp job show -g "$RG" -n "$j" --query properties.runningState -- \
        containerapp job start -g "$RG" -n "$j"
    fi
  done
}
do_webapp() {
  [ -n "$WEBAPP" ] || return 0
  if [ "$MODE" = park ]; then
    step "App Service $WEBAPP -> stopped (the B1 plan still bills)" '^stopped$' \
      webapp show -g "$RG" -n "$WEBAPP" --query state -- \
      webapp stop -g "$RG" -n "$WEBAPP"
  else
    step "App Service $WEBAPP -> running" '^running$' \
      webapp show -g "$RG" -n "$WEBAPP" --query state -- \
      webapp start -g "$RG" -n "$WEBAPP"
  fi
}
do_aks() {
  [ -n "$AKS" ] || return 0
  if [ "$MODE" = park ]; then
    step "AKS $AKS -> stopped" '^stopped$' \
      aks show -g "$RG" -n "$AKS" --query powerState.code -- \
      aks stop -g "$RG" -n "$AKS"
  else
    # Slowest start: issue it first and do not wait.
    step "AKS $AKS -> running (no-wait, takes several minutes)" '^running$' \
      aks show -g "$RG" -n "$AKS" --query powerState.code -- \
      aks start -g "$RG" -n "$AKS" --no-wait
  fi
}

print_park_summary() {
  cat <<'TXT'

Still billing while quick-parked (EUR/month, from docs/finops/cost-model.md; estimates, not measurements):
  ACR Basic                         4.40
  App Service B1 plan (stopped)     ~12   a stopped app still bills its plan
  AKS LB + public IP + OS disk      24.38 if `az aks stop` keeps LB + IP; 5.11 if released (UNVERIFIED, T-313)
  VM disk                           ~1
  Storage                           ~0.04
  Logic App (disabled)              < 0.1
  Azure SQL                         0 on the free offer (3.85 list, storage only)
  Key Vault                         < 0.03
  Cost-model parked total           ~45 (~33 with enable_app_service=false)

Deep park (human only, this script never runs terraform). From infra/terraform/envs/dev:
  terraform apply -var park=true -var enable_app_service=false -var enable_legacy_vm=false
Removes the App Service plan (~12) and the VM, disk and Logic App (~1). There is no Terraform variable
that removes AKS today; parking it for weeks means deleting the cluster (see docs/runbooks/park.md).
ACR Basic stays either way.
TXT
}

SUFFIX=""
if [ "$DRY_RUN" -eq 1 ]; then SUFFIX=" [dry-run: no az calls]"; fi
echo "== tidal $MODE (resource group: $RG)$SUFFIX =="
if [ "$MODE" = park ]; then
  # Logic App first so nothing restarts the VM; AKS last.
  do_logic
  do_vm
  do_jobs
  do_webapp
  do_aks
  print_park_summary
else
  # AKS first (slowest); VM and Logic App last.
  do_aks
  do_webapp
  do_jobs
  do_vm
  do_logic
  echo
  echo "Unparked. If a deep park was applied, revert it first (human, from infra/terraform/envs/dev):"
  echo "  terraform apply -var park=false -var enable_app_service=true -var enable_legacy_vm=true"
fi
