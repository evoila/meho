#!/bin/bash
set -e

echo "========================================="
echo "⚠️  DATABASE RESET WARNING"
echo "========================================="
echo ""
echo "This will DESTROY ALL DATA in the database!"
echo ""
read -p "Are you sure you want to continue? (type 'yes'): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Resetting database..."

# Downgrade all migrations to base
services=("meho_agent" "meho_openapi" "meho_knowledge")

echo "Downgrading all migrations to base..."
for service in "${services[@]}"; do
    echo "  Downgrading $service..."
    cd $service
    python3 -m alembic downgrade base 2>/dev/null || echo "  (no migrations to downgrade)"
    cd ..
done

echo ""
echo "Upgrading all migrations to head..."

# Upgrade all migrations
services=("meho_knowledge" "meho_openapi" "meho_agent")
for service in "${services[@]}"; do
    echo "  Upgrading $service..."
    cd $service
    python3 -m alembic upgrade head 2>/dev/null || echo "  (no migrations to upgrade)"
    cd ..
done

echo ""
echo "========================================="
echo "✓ Database reset complete!"
echo "========================================="

