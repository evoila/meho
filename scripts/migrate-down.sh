#!/bin/bash
set -e

echo "========================================="
echo "Rolling Back Database Migrations"
echo "========================================="

# Rollback in reverse order
services=("meho_agent" "meho_openapi" "meho_knowledge")

for service in "${services[@]}"; do
    echo ""
    echo "Rolling back $service..."
    cd $service
    
    if [ -d "alembic/versions" ] && [ "$(ls -A alembic/versions 2>/dev/null)" ]; then
        python3 -m alembic downgrade -1
        echo "✓ $service rolled back one revision"
    else
        echo "ℹ $service has no migrations"
    fi
    
    cd ..
done

echo ""
echo "========================================="
echo "Rollback complete!"
echo "========================================="

