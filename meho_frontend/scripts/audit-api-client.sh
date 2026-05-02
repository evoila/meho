#!/usr/bin/env bash
# Layer 1 "grep reference audit" for the api-client domain split (#263).
#
# Run from anywhere inside the repo. Prints one table showing how many
# files still reach into the legacy `lib/api-client.ts` by each of the
# known import-path variants, plus raw-axios/observability hotspots and
# the vi.mock site count.
#
# Every count should be **monotonically non-increasing** between phases.
# On the Phase 4 (#350) branch all counts should be 0.
#
# Not a CI gate — a developer aid. Include its output in each phase PR's
# description so reviewers can see the deltas at a glance.

set -uo pipefail

# Locate the frontend root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${FRONTEND_ROOT}"

if ! command -v rg >/dev/null 2>&1; then
  echo "error: ripgrep (rg) is required" >&2
  echo "  install via: brew install ripgrep  (macOS)" >&2
  echo "              apt-get install ripgrep  (Debian/Ubuntu)" >&2
  echo "              dnf install ripgrep      (Fedora)" >&2
  echo "              cargo install ripgrep    (anywhere)" >&2
  exit 2
fi

echo "api-client grep audit — ${FRONTEND_ROOT}"
echo "branch: $(git -C "${FRONTEND_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'n/a')"
echo "head:   $(git -C "${FRONTEND_ROOT}" rev-parse --short HEAD 2>/dev/null || echo 'n/a')"
echo ""

count_files() {
  local pattern="$1"
  local path="$2"
  rg -l "${pattern}" "${path}" 2>/dev/null | wc -l | tr -d ' '
}

count_hits() {
  local pattern="$1"
  local path="$2"
  rg "${pattern}" "${path}" 2>/dev/null | wc -l | tr -d ' '
}

printf "%-62s %s\n" "Pattern" "Count"
printf "%-62s %s\n" "-------" "-----"
printf "%-62s %s\n" "@/lib/api-client (files)"                "$(count_files "from '@/lib/api-client'" src)"
printf "%-62s %s\n" "../lib/api-client (files)"               "$(count_files "from '\.\./lib/api-client'" src)"
printf "%-62s %s\n" "../../lib/api-client (files)"            "$(count_files "from '\.\./\.\./lib/api-client'" src)"
printf "%-62s %s\n" "../../../lib/api-client (files)"         "$(count_files "from '\.\./\.\./\.\./lib/api-client'" src)"
printf "%-62s %s\n" "../../../../lib/api-client (files)"      "$(count_files "from '\.\./\.\./\.\./\.\./lib/api-client'" src)"
printf "%-62s %s\n" "./api-client (lib internal) (files)"     "$(count_files "from '\./api-client'" src/lib)"
printf "%-62s %s\n" "inline dynamic import('.../api-client')" "$(count_hits "import\(.*lib/api-client" src)"
printf "%-62s %s\n" "vi.mock() sites"                          "$(count_hits "vi\.mock\([\"'].*api-client" src)"
printf "%-62s %s\n" "apiClient.client.* raw axios (hits)"      "$(count_hits "apiClient\.client\." src)"
printf "%-62s %s\n" "apiClient.observability.* nested (hits)"  "$(count_hits "apiClient\.observability\." src)"
printf "%-62s %s\n" "Total api-client references (catch-all)"  "$(count_hits "api-client" src)"

echo ""
echo "Done. Re-run after each phase; numbers must not increase."
