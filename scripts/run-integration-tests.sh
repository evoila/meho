#!/bin/bash
set -e

# Ensure test environment is running
if ! docker ps | grep -q "meho-postgres-test"; then
    echo "Starting test environment..."
    ./scripts/test-env-up.sh
fi

# Run integration tests
pytest -m integration "$@"

