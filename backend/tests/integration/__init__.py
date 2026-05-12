# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for the meho backplane.

Tests in this package exercise multiple chassis subsystems end-to-end
through the production FastAPI app, against real engines (PostgreSQL
via testcontainers) rather than the per-test SQLite that the unit
suites under ``tests/`` use. The Docker-availability skip pattern from
:mod:`tests.test_migration_rollback` is reused at module level — agent
sandboxes without Docker skip the whole test class; CI runners
provision Docker and run the integration coverage.
"""
