#!/usr/bin/env bash
# Fail if any GitHub Actions step uses a tag or branch reference instead of a
# full commit SHA. All actions — including GitHub-owned ones — must be pinned;
# tags are mutable regardless of the action's owner.
#
# Usage: bash scripts/check-action-pins.sh
set -euo pipefail

WORKFLOW_DIR=".github/workflows"
FAILURES=0

while IFS= read -r -d '' file; do
    while IFS= read -r line; do
        # Skip pure comment lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue

        # Skip lines that don't contain `uses:`
        [[ "$line" == *"uses:"* ]] || continue

        # Extract the action ref using parameter expansion (avoids bash 3.2
        # capture-group bugs with [^...] character classes).
        # 1. Strip everything up to and including `uses:`
        ref="${line#*uses:}"
        # 2. Trim leading whitespace
        ref="${ref#"${ref%%[! 	]*}"}"
        # 3. Trim at first whitespace or inline `#` comment
        ref="${ref%%[ 	#]*}"

        # Skip if not an action ref (no @)
        [[ "$ref" == *"@"* ]] || continue

        # Extract the pin (everything after the last @)
        pin="${ref##*@}"

        # A valid SHA is exactly 40 hex characters
        if ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
            echo "::error file=${file}::Unpinned action: ${ref} — use a full commit SHA instead of '${pin}'"
            (( FAILURES++ )) || true
        fi
    done < "$file"
done < <(find "$WORKFLOW_DIR" -name "*.yml" -o -name "*.yaml" | sort | tr '\n' '\0')

if (( FAILURES > 0 )); then
    echo ""
    echo "Found ${FAILURES} unpinned action(s)."
    echo "Look up the SHA: gh api repos/<owner>/<repo>/git/ref/tags/<version> --jq '.object.sha'"
    exit 1
fi

echo "All actions are pinned to commit SHAs."
