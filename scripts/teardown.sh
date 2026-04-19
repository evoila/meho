#!/bin/bash
set -e

echo "========================================="
echo "MEHO Infrastructure Teardown"
echo "========================================="
echo ""

# Stop Aspire if running
echo "Stopping Aspire orchestrator..."
if pgrep -f "MehoAppHost" > /dev/null; then
    pkill -f "MehoAppHost"
    echo "✓ Aspire orchestrator stopped"
else
    echo "✓ Aspire orchestrator not running"
fi

# Stop all MEHO containers
echo ""
echo "Stopping MEHO containers..."
docker ps -a --filter "name=meho-" --format "{{.Names}}" | while read container; do
    if [[ -n "$container" ]]; then
        echo "  Stopping $container..."
        docker stop "$container" > /dev/null 2>&1 || true
        docker rm "$container" > /dev/null 2>&1 || true
    fi
done

# Stop infrastructure containers
echo ""
echo "Stopping infrastructure containers..."
INFRA_CONTAINERS=("meho-postgres" "meho-qdrant" "meho-minio" "meho-redis" "meho-rabbitmq")
for container in "${INFRA_CONTAINERS[@]}"; do
    if docker ps -a --format "{{.Names}}" | grep -q "^${container}"; then
        echo "  Stopping $container..."
        docker stop "$container" > /dev/null 2>&1 || true
        docker rm "$container" > /dev/null 2>&1 || true
    fi
done

# Optional: Remove volumes (commented out by default)
# Uncomment the following lines to also remove data volumes
# echo ""
# read -p "Remove data volumes? This will delete all data! (y/N) " -n 1 -r
# echo
# if [[ $REPLY =~ ^[Yy]$ ]]; then
#     echo "Removing volumes..."
#     docker volume rm meho-postgres-data meho-qdrant-data meho-minio-data meho-redis-data meho-rabbitmq-data 2>/dev/null || true
# fi

echo ""
echo "========================================="
echo "Teardown Complete"
echo "========================================="
echo ""
echo "All MEHO services have been stopped."
echo ""
echo "To remove Docker images, run:"
echo "  docker rmi meho-api meho-knowledge meho-openapi meho-agent meho-ingestion meho-frontend"
echo ""

