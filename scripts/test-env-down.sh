#!/bin/bash
set -e

echo "Stopping MEHO Test Environment..."
docker compose -f docker-compose.test.yml down

echo "Test environment stopped."
echo "Note: Test data is ephemeral and already cleared."

