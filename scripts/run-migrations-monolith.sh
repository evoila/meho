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

# Widen any existing Alembic version tables that use the old VARCHAR(32) default.
# Newer revision IDs (e.g. connectors_0012_dedup_custom_skills) exceed 32 chars.
uv run python -c "
import os, sqlalchemy as sa
url = os.environ['DATABASE_URL'].replace('+asyncpg', '')
engine = sa.create_engine(url)
with engine.connect() as conn:
    rows = conn.execute(sa.text(
        \"SELECT table_name FROM information_schema.tables \"
        \"WHERE table_name LIKE 'alembic_version%'\"
    )).fetchall()
    for (tbl,) in rows:
        conn.execute(sa.text(
            f'ALTER TABLE {tbl} ALTER COLUMN version_num TYPE VARCHAR(128)'
        ))
    conn.commit()
print(f'  Checked {len(rows)} alembic version table(s)')
" 2>/dev/null || true

# Array of module directories that contain migrations
# All modules now have their own alembic folders for consistency
# NOTE: Order matters! FK dependency chain: topology -> connectors -> knowledge
modules=("meho_app/modules/topology" "meho_app/modules/connectors" "meho_app/modules/knowledge" "meho_app/modules/memory" "meho_app/modules/agents" "meho_app/modules/ingestion" "meho_app/modules/scheduled_tasks" "meho_app/modules/orchestrator_skills" "meho_app/modules/audit")

for module in "${modules[@]}"; do
    echo "➡️  Checking ${module}..."
    
    if [[ -d "${module}/alembic/versions" ]] && [[ "$(ls -A ${module}/alembic/versions 2>/dev/null)" ]]; then
        echo "   Running migrations..."
        # Run from repo root with -c flag to avoid cd-ing into module dirs.
        # cd-ing into connectors/ causes `import email` (stdlib) to resolve
        # to meho_app/modules/connectors/email/ -- a circular import crash.
        uv run alembic -c "${module}/alembic.ini" upgrade head
        echo "   ✅ ${module} migrations complete"
    else
        echo "   ℹ️  ${module} has no migrations"
    fi
    echo ""
done

echo "========================================="
echo "✅ All migrations complete!"
echo "========================================="
