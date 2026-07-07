# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Database-migration tests for the meho backplane.

Tests in this package assert per-migration data-transformation logic
(backfills, product-split reconciles, seed + retire migrations), the
alembic upgrade/downgrade round-trip, and the Postgres-container
forward-compat rollback. They are relocated out of the flat ``tests/``
tree so the required unit lane can ``--ignore=tests/migrations`` and run
them only on migration-touching PRs via the ``python-migration-tests``
job. Docker-less sandboxes skip the pgvector-container tests in
:mod:`test_migration_rollback`; the SQLite-based per-migration tests run
everywhere.
"""
