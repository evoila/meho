#!/usr/bin/env bash
# Local UI-dev loop: Tailwind watch + uvicorn reload on :8800.
# Usage: bash backend/dev/run.sh   (from anywhere; paths are absolute)
#
# Prereqs (one-time, already done on this machine):
#   docker run -d --name meho-redis-dev -p 6380:6379 redis:7-alpine
#   curl -sL -o backend/dev/bin/tailwindcss \
#     https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.0/tailwindcss-macos-arm64 \
#     && chmod +x backend/dev/bin/tailwindcss
#   (the brew tailwindcss runs under node and breaks the vendored
#    DaisyUI plugin loader — use the official standalone binary)
#   cd backend && set -a && source dev/.env.dev && set +a && uv run alembic upgrade head
#
# Then open http://localhost:8800/dev/login — it mints a session and
# drops you on /ui/.
set -euo pipefail

BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker start meho-redis-dev >/dev/null 2>&1 || true

"$BACKEND/dev/bin/tailwindcss" \
  -i "$BACKEND/src/meho_backplane/ui/static/src/styles.css" \
  -o "$BACKEND/src/meho_backplane/ui/static/dist/tailwind.css" \
  --watch=always &
TAILWIND_PID=$!
trap 'kill $TAILWIND_PID 2>/dev/null' EXIT

cd "$BACKEND"
set -a
source dev/.env.dev
set +a
# No `exec` — the EXIT trap must survive to reap the Tailwind watcher.
uv run python -m uvicorn dev.devserver:app --host 127.0.0.1 --port 8800 --reload
