#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Run mypy on the monolith package (`meho_app/`).
#
# Pre-monolith versions of this script iterated over six per-service
# packages (`meho_core`, `meho_knowledge`, `meho_openapi`, `meho_agent`,
# `meho_ingestion`, `meho_api`) and aggregated their error counts. Those
# packages live only in `_archive/distributed-services/` today and the
# loop has been collapsed to a single mypy invocation.
#
# Usage:
#   ./scripts/typecheck.sh          # full output
#   ./scripts/typecheck.sh --quiet  # summary only (used by run-critical-tests.sh
#                                     and dev-env.sh's tests command)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

QUIET_MODE=false
if [[ "${1:-}" == "--quiet" ]]; then
    QUIET_MODE=true
fi

if ! $QUIET_MODE; then
    echo "Running mypy on meho_app/..."
    echo ""
fi

OUTPUT=$(uv run mypy meho_app/ --ignore-missing-imports --show-error-codes 2>&1 || true)

ERROR_COUNT=$(echo "$OUTPUT" | grep -oE "Found [0-9]+ error" | grep -oE "[0-9]+" || echo "0")

if [[ "$ERROR_COUNT" -eq 0 ]]; then
    if ! $QUIET_MODE; then
        echo "$OUTPUT"
    fi
    echo "Type checking passed (0 errors)"
    exit 0
fi

if ! $QUIET_MODE; then
    echo "$OUTPUT"
    echo ""
fi

echo "Type checking found $ERROR_COUNT error(s)"
if $QUIET_MODE; then
    echo "Run './scripts/typecheck.sh' (without --quiet) to see all errors"
fi
exit 1
