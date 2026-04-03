#!/bin/bash
set -e

echo "========================================="
echo "Starting MEHO Test Environment"
echo "========================================="

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "Error: Docker is not running. Please start Docker and try again."
    exit 1
fi

# Start test services
echo "Starting test infrastructure services..."
docker compose -f docker-compose.test.yml up -d --wait

echo "Test environment is ready!"
echo ""
echo "Services:"
echo "  PostgreSQL (test): localhost:5432 (database: meho_test)"
echo "  MinIO (test):      http://localhost:9000"
echo "  Redis (test):      localhost:6379"
echo "  Keycloak (test):   http://localhost:8080"
echo ""

