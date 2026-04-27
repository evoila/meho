#!/usr/bin/env bash
# Check if database tables exist (migrations were run)
# This is a fail-fast check to prevent running without migrations

set -e

DB_HOST="${DATABASE_HOST:-localhost}"
DB_PORT="${DATABASE_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-meho}"
DB_USER="${POSTGRES_USER:-meho}"
DB_PASS="${POSTGRES_PASSWORD:-password}"

# Check if knowledge_chunk table exists
if ! PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -tAc "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='knowledge_chunk'" | grep -q 1; then
    echo "❌ ERROR: Database tables not found!"
    echo "   Migrations have not been run."
    echo ""
    echo "   Please stop the services and restart using:"
    echo "     ./scripts/dev-env.sh down"
    echo "     ./scripts/dev-env.sh up"
    echo ""
    echo "   DO NOT use 'docker-compose up' directly!"
    exit 1
fi

echo "✅ Database migrations verified"
exit 0

