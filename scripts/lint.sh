#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Run code quality checks against the monolith package (`meho_app/`).
#
# Pre-monolith versions of this script invoked ruff and mypy against six
# now-deleted packages (`meho_core`, `meho_knowledge`, `meho_openapi`,
# `meho_agent`, `meho_ingestion`, `meho_api`). Those names live only in the
# `_archive/distributed-services/` snapshot today; pointing the linter at
# them in CI would be a no-op at best and a confusing failure at worst.
#
# Usage:
#   ./scripts/lint.sh           # check + format-check + types
#   ./scripts/lint.sh --fix     # apply ruff --fix and ruff format

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ "${1:-}" == "--fix" ]]; then
    echo "Applying ruff --fix and ruff format..."
    uv run ruff check --fix meho_app/ tests/ scripts/
    uv run ruff format meho_app/ tests/ scripts/
    exit 0
fi

echo "========================================="
echo "Running code quality checks"
echo "========================================="

echo ""
echo "1. ruff check"
uv run ruff check meho_app/ tests/ scripts/

echo ""
echo "2. ruff format --check"
uv run ruff format --check meho_app/ tests/ scripts/

echo ""
echo "3. mypy --strict (meho_app/)"
uv run mypy meho_app/ --ignore-missing-imports

echo ""
echo "========================================="
echo "All quality checks passed"
echo "========================================="
echo ""
echo "Auto-fix command:"
echo "  ./scripts/lint.sh --fix"
