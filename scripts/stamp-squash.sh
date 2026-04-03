#!/bin/bash
# ============================================================================
# stamp-squash.sh — One-time stamp for existing MEHO deployments
# ============================================================================
#
# PURPOSE:
# When upgrading an existing MEHO deployment to the squashed migration set,
# run this script ONCE to stamp all 9 module alembic_version tables to the
# squash_001 revision. This tells Alembic "these modules are already at the
# squashed state" so it won't attempt to re-run DDL that already exists.
#
# The squash migrations themselves have two-path detection (they check if
# the main table exists and skip DDL if so), so `alembic upgrade head` is
# also safe. This script is the explicit, belt-and-suspenders alternative
# for operators who want certainty.
#
# USAGE:
#   # Using DATABASE_URL environment variable:
#   export DATABASE_URL="postgresql://meho:password@localhost:5432/meho"
#   bash scripts/stamp-squash.sh
#
#   # Or pass it directly:
#   bash scripts/stamp-squash.sh --database-url "postgresql://meho:password@localhost:5432/meho"
#
# WHEN TO USE:
# - After upgrading to the squashed migration codebase
# - On an existing deployment that already has all tables created
# - Run ONCE, then future `alembic upgrade head` works normally
#
# ============================================================================

set -e

# Parse optional --database-url argument
while [[ $# -gt 0 ]]; do
    case $1 in
        --database-url)
            export DATABASE_URL="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: bash scripts/stamp-squash.sh [--database-url URL]"
            exit 1
            ;;
    esac
done

if [[ -z "$DATABASE_URL" ]]; then
    echo "ERROR: DATABASE_URL not set. Either export it or pass --database-url."
    exit 1
fi

echo "========================================="
echo "Stamping MEHO modules to squash_001"
echo "========================================="
echo ""
echo "Database: ${DATABASE_URL%%@*}@***"
echo ""

# FK ordering: knowledge -> topology -> connectors -> memory -> agents ->
#              ingestion -> scheduled_tasks -> orchestrator_skills -> audit

echo "Stamping knowledge..."
cd meho_app/modules/knowledge && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: knowledge -> squash_001"

echo "Stamping topology..."
cd meho_app/modules/topology && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: topology -> squash_001"

echo "Stamping connectors..."
cd meho_app/modules/connectors && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: connectors -> squash_001"

echo "Stamping memory..."
cd meho_app/modules/memory && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: memory -> squash_001"

echo "Stamping agents..."
cd meho_app/modules/agents && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: agents -> squash_001"

echo "Stamping ingestion..."
cd meho_app/modules/ingestion && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: ingestion -> squash_001"

echo "Stamping scheduled_tasks..."
cd meho_app/modules/scheduled_tasks && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: scheduled_tasks -> squash_001"

echo "Stamping orchestrator_skills..."
cd meho_app/modules/orchestrator_skills && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: orchestrator_skills -> squash_001"

echo "Stamping audit..."
cd meho_app/modules/audit && python3 -m alembic stamp squash_001 && cd - > /dev/null
echo "  Done: audit -> squash_001"

echo ""
echo "========================================="
echo "All 9 modules stamped to squash_001"
echo "========================================="
echo ""
echo "You can now run 'alembic upgrade head' normally for future migrations."
