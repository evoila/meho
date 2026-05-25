# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schema drift guard for :class:`~meho_backplane.db.models.ApprovalRequest`.

Mirrors the pattern :mod:`tests.test_db_agent_run` uses for AgentRun —
asserts that the ORM enum values and the migration's literal vocabulary
tuple stay in lock-step. A mismatch means either the migration or the
ORM model was updated without updating the other.

This module intentionally imports the private ``_APPROVAL_STATUSES`` tuple
from the models module directly — that's the canonical coupling point, just
as the migration references it as a frozen snapshot.
"""

from meho_backplane.db.models import (  # type: ignore[attr-defined]
    _APPROVAL_STATUSES,
    ApprovalStatus,
)


def test_status_check_matches_enum() -> None:
    """The ORM ApprovalStatus enum and _APPROVAL_STATUSES tuple must be equal."""
    enum_values = {s.value for s in ApprovalStatus}
    tuple_values = set(_APPROVAL_STATUSES)
    assert enum_values == tuple_values, (
        f"Drift detected between ApprovalStatus and _APPROVAL_STATUSES.\n"
        f"In enum only: {enum_values - tuple_values}\n"
        f"In tuple only: {tuple_values - enum_values}"
    )
