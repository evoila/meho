# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Helper for migration tests that call live lookup code on an old schema.

The live lookup code (``count_known_ops`` and friends in
``operations/_lookup.py``) also reads ``tenant_operation_setting``
(migration ``0120``, #5029). A test that stops at an older revision and
then calls that code needs the table to exist. :func:`tenant_setting_table`
creates it (empty, so every tenant uses the install default) for the
duration of the probe and drops it again, so a later
``alembic upgrade head`` can still create it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine

from meho_backplane.db.models import TenantOperationSetting

__all__ = ["tenant_setting_table"]


@contextmanager
def tenant_setting_table(sync_url: str) -> Iterator[None]:
    """Create ``tenant_operation_setting`` if missing; drop it afterwards if created here."""
    engine = create_engine(sync_url)
    table = TenantOperationSetting.__table__
    try:
        with engine.begin() as conn:
            created = not engine.dialect.has_table(conn, table.name)
            if created:
                table.create(conn)
        yield
        if created:
            with engine.begin() as conn:
                table.drop(conn)
    finally:
        engine.dispose()
