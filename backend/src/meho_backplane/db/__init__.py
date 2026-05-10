# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL persistence layer for the backplane.

This package owns the SQLAlchemy 2.x async engine, the per-request
session factory, and the DB-migration-state readiness probe that
compares the database's current Alembic revision to the head revision
on disk. v0.1 ships an empty migration history (T28 lands the first
migration); the probe still flips ``/ready`` red until ``alembic
upgrade head`` has been applied — a deliberately fail-closed default
that matches the chassis-level discipline established in T19.
"""

__all__: list[str] = []
