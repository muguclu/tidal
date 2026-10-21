#!/usr/bin/env bash
# Configure repo merge settings and `main` branch protection (T-007, SPEC-08 §1/§3).
#
# Usage:
#   scripts/protect-main.sh            dry run: print the JSON that would be sent (default)
#   scripts/protect-main.sh --apply    apply settings + protection (idempotent PATCH/PUT)
#   scripts/protect-main.sh --verify   GET current state and diff against desired state
#
# Env: REPO (default muguclu/tidal), BRANCH (default main).
# Requires: gh (authenticated; admin on the repo for --apply). No jq needed.
#
# Design notes
# - Required approving reviews = 0: this is a solo-owner repo and GitHub forbids approving your
#   own PR, so a review count >= 1 would make main unmergeable. The review gate is the required
#   `review-checklist` status check instead (SPEC-08 §1 solo note, option 2). A PR is still required.
# - enforce_admins = false so the owner can recover from a broken protection/CI state.
# - Signed commits are NOT required: agent commits on the dev box are unsigned. This deviates from
#   SPEC-08 §1 ("signed commits"); revisit once commit signing is set up.
set -euo pipefail

REPO="${REPO:-muguclu/tidal}"
BRANCH="${BRANCH:-main}"
MODE="dry-run"

for arg in "$@"; do
  case "$arg" in
    --apply) MODE="apply" ;;
    --verify) MODE="verify" ;;
    --dry-run) MODE="dry-run" ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

command -v gh >/dev/null || { echo "gh not found" >&2; exit 1; }
# Check names must match `name:` of jobs in ci.yml / review-checklist.yml.
CHECKS=(lint types tests coverage secrets-scan contracts openapi-diff review-checklist)
CHECKS_JSON=$(printf '"%s",' "${CHECKS[@]}")
CHECKS_JSON="[${CHECKS_JSON%,}]"

SETTINGS_JSON=$(cat <<JSON
{
  "allow_squash_merge": true,
  "allow_merge_commit": false,
  "allow_rebase_merge": false,
  "delete_branch_on_merge": true,
  "squash_merge_commit_title": "PR_TITLE",
  "squash_merge_commit_message": "PR_BODY"
}
JSON
)

PROTECTION_JSON=$(cat <<JSON
{
  "required_status_checks": { "strict": true, "contexts": $CHECKS_JSON },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
)

case "$MODE" in
  dry-run)
    echo "# DRY RUN (use --apply to send). Repo: $REPO, branch: $BRANCH"
    echo "# PATCH repos/$REPO"
    echo "$SETTINGS_JSON"
    echo "# PUT repos/$REPO/branches/$BRANCH/protection"
    echo "$PROTECTION_JSON"
    ;;
  apply)
    echo "Applying repo settings to $REPO ..."
    echo "$SETTINGS_JSON" | gh api -X PATCH "repos/$REPO" --input - >/dev/null
    echo "Applying branch protection to $REPO:$BRANCH ..."
    echo "$PROTECTION_JSON" | gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" --input - >/dev/null
    echo "Done. Run with --verify to confirm."
    ;;
  verify)
    rc=0
    # Compare flat key=value lines (gh --jq needs no external jq).
    want_settings=$(cat <<EOF2
allow_merge_commit=false
allow_rebase_merge=false
allow_squash_merge=true
delete_branch_on_merge=true
squash_merge_commit_message=PR_BODY
squash_merge_commit_title=PR_TITLE
EOF2
)
    actual_settings=$(gh api "repos/$REPO" --jq '
      ["allow_merge_commit=\(.allow_merge_commit)", "allow_rebase_merge=\(.allow_rebase_merge)",
       "allow_squash_merge=\(.allow_squash_merge)", "delete_branch_on_merge=\(.delete_branch_on_merge)",
       "squash_merge_commit_message=\(.squash_merge_commit_message)",
       "squash_merge_commit_title=\(.squash_merge_commit_title)"] | .[]')
    want_prot=$( {
      printf 'allow_deletions=false
allow_force_pushes=false
enforce_admins=false
'
      printf 'required_approving_review_count=0
required_conversation_resolution=true
'
      printf 'required_linear_history=true
strict=true
'
      printf '%s
' "${CHECKS[@]}" | sort | sed 's/^/check=/'
    } | sort )
    actual_prot=$(gh api "repos/$REPO/branches/$BRANCH/protection" --jq '
      ["allow_deletions=\(.allow_deletions.enabled)", "allow_force_pushes=\(.allow_force_pushes.enabled)",
       "enforce_admins=\(.enforce_admins.enabled)",
       "required_approving_review_count=\(.required_pull_request_reviews.required_approving_review_count)",
       "required_conversation_resolution=\(.required_conversation_resolution.enabled)",
       "required_linear_history=\(.required_linear_history.enabled)",
       "strict=\(.required_status_checks.strict)"]
      + [.required_status_checks.contexts[] | "check=\(.)"] | .[]' | sort)
    diff <(echo "$want_settings" | sort) <(echo "$actual_settings" | sort) || rc=1
    diff <(echo "$want_prot") <(echo "$actual_prot") || rc=1
    if [ "$rc" -eq 0 ]; then echo "OK: $REPO matches desired state"; else echo "DRIFT detected (< desired, > actual)" >&2; fi
    exit "$rc"
    ;;
esac
