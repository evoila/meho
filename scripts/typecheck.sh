#!/usr/bin/env bash
#
# Run mypy type checking on all MEHO services
# This catches method signature mismatches at code-writing time
#
# Usage:
#   ./scripts/typecheck.sh          # Full output
#   ./scripts/typecheck.sh --quiet  # Summary only
#

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

# Parse arguments
QUIET_MODE=false
if [[ "${1:-}" == "--quiet" ]]; then
    QUIET_MODE=true
fi

# Activate virtual environment if it exists
if [[ -f ".venv/bin/activate" ]]; then
    source .venv/bin/activate
fi

# Check if mypy is installed
if ! command -v mypy &> /dev/null; then
    echo "❌ mypy not found. Install it: pip install mypy"
    exit 1
fi

if ! $QUIET_MODE; then
    echo "🔍 Running type checking on all services..."
    echo ""
fi

MODULES=(
    "meho_core"
    "meho_ingestion"
    "meho_openapi"
    "meho_knowledge"
    "meho_api"
    "meho_agent"
)

TOTAL_ERRORS=0
ERRORS_SUMMARY=""

for module in "${MODULES[@]}"; do
    if ! $QUIET_MODE; then
        echo "Checking $module..."
    fi
    
    # Run mypy and capture output
    OUTPUT=$(mypy "$module" --ignore-missing-imports --show-error-codes 2>&1 || true)
    
    # Extract error count from output
    ERROR_COUNT=$(echo "$OUTPUT" | grep -oE "Found [0-9]+ error" | grep -oE "[0-9]+" || echo "0")
    
    if [[ "$ERROR_COUNT" -gt 0 ]]; then
        TOTAL_ERRORS=$((TOTAL_ERRORS + ERROR_COUNT))
        ERRORS_SUMMARY="${ERRORS_SUMMARY}  • $(printf '%-18s' "$module:")  ${ERROR_COUNT} errors\n"
        
        if ! $QUIET_MODE; then
            echo "$OUTPUT"
            echo ""
        fi
    else
        if ! $QUIET_MODE; then
            echo "  ✅ No errors"
            echo ""
        fi
    fi
done

# Summary
echo ""
if [[ $TOTAL_ERRORS -eq 0 ]]; then
    echo "✅ Type checking passed! (0 errors)"
    exit 0
else
    echo "❌ Type checking found $TOTAL_ERRORS error(s):"
    echo ""
    echo -e "$ERRORS_SUMMARY"
    if $QUIET_MODE; then
        echo "Run './scripts/typecheck.sh' (without --quiet) to see all errors"
    else
        echo "Fix these errors before proceeding."
    fi
    exit 1
fi
