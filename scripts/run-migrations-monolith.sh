#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Runs all MEHO database migrations in a single linear history.
#
# After consolidation (Goal #294 / Issue #299) there is exactly one Alembic
# tree at meho_app/alembic/. This script is a thin wrapper that fails loud:
# any non-zero exit from alembic propagates and aborts container startup.

set -euo pipefail

echo "========================================="
echo "Running MEHO migrations (unified Alembic)"
echo "========================================="

cd "$(dirname "$0")/.."

# Refuse to run if a stale per-module alembic/ directory is still on disk.
# After Goal #294 / #299, those nine trees were collapsed into meho_app/alembic/.
# A fresh clone never trips this; an in-place upgrade onto a clone with leftover
# untracked artifacts (e.g. __pycache__/env.cpython-313.pyc) might. Failing loud
# avoids subtle bytecode-cache resolution to the legacy env.py at runtime.
# See #465 for the rescue-script counterpart that also offers --clean-host-paths.
if [ ! -d meho_app/modules ]; then
    echo "ERROR: meho_app/modules/ is missing. Are you running this from the repo root?"
    exit 1
fi
STALE_ALEMBIC_DIRS=$(find meho_app/modules -maxdepth 2 -type d -name alembic)
if [ -n "$STALE_ALEMBIC_DIRS" ]; then
    echo "ERROR: legacy per-module alembic/ directories detected:"
    printf '%s\n' "$STALE_ALEMBIC_DIRS" | sed 's/^/  /'
    echo ""
    echo "These were removed by Goal #294 / #299. Untracked artifacts (typically"
    echo "__pycache__/) may be lingering. Either:"
    echo "  - run scripts/migrate_to_unified_alembic.py --clean-host-paths (#465), or"
    echo "  - remove them manually: rm -rf <listed paths>"
    exit 1
fi

uv run alembic -c meho_app/alembic.ini upgrade head

echo "✅ All migrations complete!"
