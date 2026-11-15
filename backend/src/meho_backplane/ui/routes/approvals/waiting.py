# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approvals page: the requests this person may approve come first.

On the Pending tab the console lists the requests the signed-in person may
approve ("waiting for you") before the rest, so on a phone the requests
that need this person sit at the top. The check is
:func:`~meho_backplane.operations.approval_rights.approve_right`, the same
one the detail modal and the approve itself use. The order inside each
group stays as the database returned it, so a change ticket's steps keep
their oldest-first order. The route reads every pending request in
batches and feeds each batch to a :class:`WaitingFirstPager` before it cuts
the page, so "Load more" goes on with the same order however long the
queue is.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_rights import approve_right

__all__ = ["WaitingFirstPager"]


class WaitingFirstPager:
    """Cut one page of "waiting for you first" out of batches of rows.

    The route reads the pending queue in batches and passes each one to
    :meth:`add`, in the database order. The pager keeps only the first
    ``offset + size + 1`` rows of each group, which is all a page at
    *offset* can need. So memory stays small for a long queue, and a
    request waiting for this person is still on page one when hundreds of
    newer requests are not. With *only_waiting* (the "For you" tab) the
    other group stays empty, so the page holds only the requests waiting
    for this person.
    """

    def __init__(
        self, operator: Operator, *, offset: int, size: int, only_waiting: bool = False
    ) -> None:
        self._operator = operator
        self._only_waiting = only_waiting
        self._offset = offset
        self._size = size
        self._keep = offset + size + 1
        self._waiting: list[ApprovalRequest] = []
        self._others: list[ApprovalRequest] = []

    def add(self, rows: Sequence[ApprovalRequest]) -> None:
        """Split one batch of *rows* into the two groups."""
        for row in rows:
            waiting = approve_right(self._operator, row).may_approve
            if not waiting and self._only_waiting:
                continue
            group = self._waiting if waiting else self._others
            if len(group) < self._keep:
                group.append(row)

    def page(self) -> tuple[list[ApprovalRequest], bool, set[uuid.UUID]]:
        """Return ``(rows, has_more, waiting ids)`` for the page."""
        ordered = (self._waiting + self._others)[: self._keep]
        end = self._offset + self._size
        rows = ordered[self._offset : end]
        waiting_ids = {row.id for row in self._waiting}
        return rows, len(ordered) > end, {row.id for row in rows if row.id in waiting_ids}
