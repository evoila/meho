# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which release wrote the built-in operation rows (#5019).

Every start writes the built-in operation rows (``typed`` and
``composite`` rows of ``endpoint_descriptor``) from the code of the
running release: which code runs, the parameter rules, the risk level
and the approval flag. All pods share these rows. When two releases run
at the same time (during an update), the rows hold the values of the
release that started last, and the other pods serve them.

:func:`record_catalog_release` stores the release in the one-row table
``operation_catalog_state``, inside the same transaction as the
operation rows. :func:`catalog_release_status` compares the stored
release with the release of this pod. ``GET /api/v1/health`` reports
the result, and a pod that serves rows from another release logs
``operation_catalog_written_by_other_release``.

The release is the commit hash of the build (``GIT_SHA``, the
``git_sha`` field of ``GET /version``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationCatalogState
from meho_backplane.version import build_git_sha

__all__ = ["CatalogReleaseStatus", "catalog_release_status", "record_catalog_release"]

_log = structlog.get_logger(__name__)

_STATE_ROW_ID = 1

#: ``same_release``: this pod's release wrote the rows.
#: ``other_release``: another release wrote them.
#: ``not_recorded``: no start of a release with this check has written them yet.
CatalogReleaseState = Literal["same_release", "other_release", "not_recorded"]


@dataclass(frozen=True)
class CatalogReleaseStatus:
    """The stored catalog release compared with the running release."""

    state: CatalogReleaseState
    this_release: str
    written_by_release: str | None
    written_at: datetime | None


async def record_catalog_release(session: AsyncSession) -> None:
    """Store this pod's release as the writer of the operation rows.

    Runs in *session* and does not commit: the caller commits it together
    with the operation rows. One ``INSERT ... ON CONFLICT DO UPDATE`` on
    the one-row table, so two pods that start at once do not fail on the
    insert.
    """
    await session.execute(
        text(
            "INSERT INTO operation_catalog_state (id, written_by_release, written_at) "
            "VALUES (:id, :release, :now) "
            "ON CONFLICT (id) DO UPDATE SET "
            "written_by_release = excluded.written_by_release, "
            "written_at = excluded.written_at"
        ).bindparams(bindparam("now", type_=DateTime(timezone=True))),
        {"id": _STATE_ROW_ID, "release": build_git_sha(), "now": datetime.now(UTC)},
    )


async def catalog_release_status() -> CatalogReleaseStatus:
    """Read the stored release and compare it with this pod's release.

    Logs ``operation_catalog_written_by_other_release`` when they differ:
    this pod then serves operation settings that another release wrote.
    """
    this_release = build_git_sha()
    async with get_sessionmaker()() as session:
        row = await session.get(OperationCatalogState, _STATE_ROW_ID)
    if row is None:
        return CatalogReleaseStatus(
            state="not_recorded",
            this_release=this_release,
            written_by_release=None,
            written_at=None,
        )
    written_by, written_at = row.written_by_release, row.written_at
    if written_by == this_release:
        state: CatalogReleaseState = "same_release"
    else:
        state = "other_release"
        _log.warning(
            "operation_catalog_written_by_other_release",
            this_release=this_release,
            written_by_release=written_by,
            written_at=written_at.isoformat(),
            doc="docs/acceptance/rollback.md",
        )
    return CatalogReleaseStatus(
        state=state,
        this_release=this_release,
        written_by_release=written_by,
        written_at=written_at,
    )
