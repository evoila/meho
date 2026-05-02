#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Container entrypoint: run migrations, then exec the application command.
#
# IMPORTANT: existing deployments upgrading from the pre-#299 multi-tree
# Alembic layout MUST run scripts/migrate_to_unified_alembic.py against
# the database BEFORE the new container starts, e.g.:
#
#     DATABASE_URL=...  python scripts/migrate_to_unified_alembic.py
#
# This entrypoint INTENTIONALLY does not auto-invoke the rescue script:
# legacy-bookkeeping rewrites are operator-driven, single-shot actions
# that must be reflected in the change log. If you have not run the
# rescue script and you are on the legacy layout, the unified
# `alembic upgrade head` below will fail loud against pre-existing
# tables -- which is the desired behaviour.

set -e

echo "Running database migrations..."
cd /app
bash scripts/run-migrations-monolith.sh

echo "Starting application..."
exec "$@"
