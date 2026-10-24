#!/usr/bin/env bash
# Export the live RBAC state of the Tidal dev environment as normalized, sorted JSON (T-403, SPEC-06 section 7).
#
#   scripts/rbac-export.sh                       # JSON on stdout
#   scripts/rbac-export.sh --check FILE          # diff against FILE (docs/rbac/expected.json), exit 1 on drift
#
# Read-only: it only calls list/show/GET. Needs `az login` as a principal that has
#   - Reader on the subscription (role assignments, identities, federated credentials), and
#   - Microsoft Graph Application.Read.All (app registrations, app role assignments, credentials).
#
# Environment (all optional):
#   TIDAL_RESOURCE_GROUP  resource group to export                    (default rg-tidal-dev)
#   RBAC_APPS             space separated app display names to export (default: the five Tidal apps)
#   GITHUB_REPOSITORY     owner/repo; replaced by <repo> in federated subjects so expected.json is repo independent
#
# Normalization: scopes are lower-cased, the subscription and resource group are dropped, and the globally unique
# top-level names (storage account, Service Bus namespace, Key Vault, registry, AKS cluster, web app) become "*".
# Users and groups become <user> and <group>. Secret values never leave Azure: only credential metadata counts.
set -euo pipefail

check_file=""
if [ "${1:-}" = "--check" ]; then
  check_file="${2:?usage: rbac-export.sh [--check expected.json]}"
elif [ $# -gt 0 ]; then
  echo "usage: rbac-export.sh [--check expected.json]" >&2
  exit 64
fi

rg="${TIDAL_RESOURCE_GROUP:-rg-tidal-dev}"
apps="${RBAC_APPS:-tidal-api tidal-cli tidal-query sp-tidal-ci sp-tidal-deploy}"
repo="${GITHUB_REPOSITORY:-}"

for tool in az jq; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 69; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

sub="$(az account show --query id -o tsv)"
rg_scope="$(printf '/subscriptions/%s/resourcegroups/%s' "$sub" "$rg" | tr '[:upper:]' '[:lower:]')"

# --- Azure RBAC ----------------------------------------------------------------------------------
az role assignment list --all --subscription "$sub" \
  --query '[].{principalId:principalId, principalType:principalType, role:roleDefinitionName, scope:scope}' \
  -o json >"$work/ra.json"

# Resolve every distinct non-human principal to its display name (managed identities, the kubelet identity, the
# Logic App identity and the CI/deploy service principals are all service principals in Entra).
: >"$work/names.jsonl"
jq -r --arg rg "$rg_scope" \
  '.[] | select((.scope | ascii_downcase | startswith($rg)) and .principalType != "User" and .principalType != "Group") | .principalId' \
  "$work/ra.json" | sort -u | while IFS= read -r pid; do
  name="$(az ad sp show --id "$pid" --query displayName -o tsv 2>/dev/null || true)"
  jq -nc --arg id "$pid" --arg name "${name:-unresolved:$pid}" '{key: $id, value: $name}' >>"$work/names.jsonl"
done
jq -s 'from_entries' "$work/names.jsonl" >"$work/names.json"

jq -S --arg rg "$rg_scope" --slurpfile names "$work/names.json" '
  def wild: ["microsoft.storage/storageaccounts", "microsoft.servicebus/namespaces", "microsoft.keyvault/vaults",
             "microsoft.containerregistry/registries", "microsoft.containerservice/managedclusters", "microsoft.web/sites"];
  def norm_scope:
    ascii_downcase
    | if . == $rg then "resourcegroup"
      elif startswith($rg + "/providers/") then
        ltrimstr($rg + "/providers/")
        | split("/") as $p
        | ($p[0] + "/" + $p[1]) as $type
        | if (wild | index($type)) != null then ([$p[0], $p[1], "*"] + $p[3:]) else $p end
        | join("/")
      else . end;
  [ .[]
    | select(.scope | ascii_downcase | startswith($rg))
    | { principal: (if .principalType == "User" then "<user>"
                    elif .principalType == "Group" then "<group>"
                    else ($names[0][.principalId] // "unresolved:" + .principalId) end),
        role: .role,
        scope: (.scope | norm_scope) } ]
  | unique | sort_by(.scope, .role, .principal)' "$work/ra.json" >"$work/assignments.json"

# --- Managed identity federated credentials (AKS workload identity, KEDA) ---------------------------------
az identity list -g "$rg" --query '[].name' -o tsv | sort | while IFS= read -r idn; do
  [ -n "$idn" ] || continue
  az identity federated-credential list --identity-name "$idn" -g "$rg" \
    --query '[].{name:name, subject:subject}' -o json |
    jq -c --arg id "$idn" '{key: $id, value: (sort_by(.name))}'
done | jq -s 'from_entries' >"$work/mi-creds.json"

# --- Entra: app role assignments and app credentials ------------------------------------------------------
graph="https://graph.microsoft.com/v1.0"
: >"$work/apps.jsonl"
: >"$work/assigned.jsonl"
for app in $apps; do
  az ad app list --display-name "$app" -o json |
    jq -c --arg n "$app" '[.[] | select(.displayName == $n)][0] // null' >"$work/app.json"
  if [ "$(cat "$work/app.json")" = "null" ]; then
    jq -nc --arg n "$app" '{key: $n, value: {missing: true}}' >>"$work/apps.jsonl"
    continue
  fi
  app_oid="$(jq -r '.id' "$work/app.json")"
  az ad app federated-credential list --id "$app_oid" -o json >"$work/fic.json"
  jq -c --arg n "$app" --arg repo "$repo" --slurpfile fic "$work/fic.json" '
    { key: $n,
      value: {
        # Metadata only (display name and type); an entry in either list is drift.
        passwordCredentials: [(.passwordCredentials // [])[] | {displayName: (.displayName // ""), type: "password"}] | sort_by(.displayName),
        keyCredentials: [(.keyCredentials // [])[] | {displayName: (.displayName // ""), type: (.type // "certificate")}] | sort_by(.displayName),
        federatedCredentials: [$fic[0][] | {name: .name, subject: (if $repo == "" then .subject else (.subject | gsub($repo; "<repo>")) end)}] | sort_by(.name)
      } }' "$work/app.json" >>"$work/apps.jsonl"

  # Who holds which app role on this app (only apps that expose roles have a meaningful list).
  sp_id="$(az ad sp list --filter "displayName eq '$app'" --query '[0].id' -o tsv)"
  [ -n "$sp_id" ] || continue
  az ad sp show --id "$sp_id" --query 'appRoles[].{id:id, value:value}' -o json >"$work/roles.json"
  az rest --method get --url "$graph/servicePrincipals/$sp_id/appRoleAssignedTo?\$top=999" --query 'value' -o json >"$work/ato.json"
  jq -c --arg n "$app" --slurpfile roles "$work/roles.json" '
    { key: $n,
      value: ([.[] | { principal: (if .principalType == "Group" then "<group>"
                                    elif .principalType == "User" then "<user>"
                                    else .principalDisplayName end),
                       role: (. as $a | ([$roles[0][] | select(.id == $a.appRoleId) | .value][0] // "unresolved:" + $a.appRoleId)) }]
               | unique | sort_by(.role, .principal)) }' "$work/ato.json" >>"$work/assigned.jsonl"
done

jq -s 'from_entries' "$work/apps.jsonl" >"$work/apps.json"
jq -s 'from_entries' "$work/assigned.jsonl" >"$work/assigned.json"

jq -S -n \
  --slurpfile ra "$work/assignments.json" \
  --slurpfile mi "$work/mi-creds.json" \
  --slurpfile apps "$work/apps.json" \
  --slurpfile ato "$work/assigned.json" \
  '{ schema: 1,
     roleAssignments: $ra[0],
     managedIdentityFederatedCredentials: $mi[0],
     entra: { apps: $apps[0], appRoleAssignedTo: $ato[0] } }' >"$work/out.json"

if [ -z "$check_file" ]; then
  cat "$work/out.json"
  exit 0
fi

# Compare as canonical JSON so formatting never counts as drift.
jq -S . "$check_file" >"$work/expected.json"
if diff -u --label "expected ($check_file)" --label "live ($rg)" "$work/expected.json" "$work/out.json"; then
  echo "RBAC export matches $check_file"
else
  echo "RBAC drift: live state differs from $check_file (see diff above)" >&2
  exit 1
fi
