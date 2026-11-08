# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approvals page: the requests this person may approve come first.

On the Pending tab the console lists the requests the signed-in person may
approve ("waiting for you") before the rest, so on a phone the requests
that need this person sit at the top. The check is
:func:`~meho_backplane.operations.approval_rights.approve_right`, the same
one the detail modal and the approve itself use. The order inside each
group stays as the database returned it, so a change ticket's steps keep
their oldest-first order. The route splits every pending request before
it cuts the page, so "Load more" goes on with the same order.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_rights import approve_right

__all__ = ["waiting_for_me_first"]


def waiting_for_me_first(
    rows: Sequence[ApprovalRequest],
    operator: Operator | None,
) -> tuple[list[ApprovalRequest], set[uuid.UUID]]:
    """Return *rows* with the ones *operator* may approve first, and their ids.

    *operator* is ``None`` when the person could not be checked; then the
    rows keep their order and none is marked, so the page never says
    "waiting for you" without a real check.
    """
    if operator is None:
        return list(rows), set()
    waiting = {row.id for row in rows if approve_right(operator, row).may_approve}
    # sorted() is stable: each group keeps the order the database returned.
    return sorted(rows, key=lambda row: row.id not in waiting), waiting
