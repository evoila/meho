#!/bin/bash
# Migration script for MEHO modular monolith
# Runs all database migrations from the unified service container

set -e

echo "========================================="
echo "Running MEHO Monolith Migrations"
echo "========================================="
echo ""
echo "All services now share the same database"
echo "Running migrations for all modules..."
echo ""

# Array of module directories that contain migrations
# All modules now have their own alembic folders for consistency
# NOTE: Order matters! topology must run before connectors (foreign key dependency)
modules=("meho_app/modules/knowledge" "meho_app/modules/topology" "meho_app/modules/connectors" "meho_app/modules/memory" "meho_app/modules/agents" "meho_app/modules/ingestion" "meho_app/modules/scheduled_tasks" "meho_app/modules/orchestrator_skills" "meho_app/modules/audit")

for module in "${modules[@]}"; do
    echo "➡️  Checking ${module}..."
    
    if [[ -d "${module}/alembic/versions" ]] && [[ "$(ls -A ${module}/alembic/versions 2>/dev/null)" ]]; then
        echo "   Running migrations..."
        # Run from repo root with -c flag to avoid cd-ing into module dirs.
        # cd-ing into connectors/ causes `import email` (stdlib) to resolve
        # to meho_app/modules/connectors/email/ -- a circular import crash.
        python3 -m alembic -c "${module}/alembic.ini" upgrade head
        echo "   ✅ ${module} migrations complete"
    else
        echo "   ℹ️  ${module} has no migrations"
    fi
    echo ""
done

echo "========================================="
echo "✅ All migrations complete!"
echo "========================================="
