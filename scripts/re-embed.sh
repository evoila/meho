#!/usr/bin/env bash
# Wrapper to run re-embedding script inside the MEHO Docker container.
#
# Usage:
#   ./scripts/re-embed.sh --dry-run    # Preview
#   ./scripts/re-embed.sh              # Full re-embed
#   ./scripts/re-embed.sh --knowledge-only --batch-size 500
set -euo pipefail

CONTAINER_NAME="meho"  # Adjust if your container name differs

# Find the running MEHO container
CONTAINER_ID=$(docker ps --filter "name=${CONTAINER_NAME}" --format '{{.ID}}' | head -1)

if [ -z "$CONTAINER_ID" ]; then
    echo "ERROR: No running container matching '${CONTAINER_NAME}'"
    echo "Start the dev environment first: ./scripts/dev-env.sh up"
    exit 1
fi

echo "Running re-embed in container ${CONTAINER_ID}..."
docker exec -it "$CONTAINER_ID" python scripts/re_embed_voyage.py "$@"
