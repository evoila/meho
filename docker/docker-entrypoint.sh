#!/bin/bash
set -e

echo "Running database migrations..."
cd /app
bash scripts/run-migrations-monolith.sh

echo "Starting application..."
exec "$@"
