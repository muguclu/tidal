#!/usr/bin/env bash
# unpark.sh - reverse of park.sh (T-313): AKS first (slowest), VM and Logic App last.
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/park.sh" --unpark "$@"
