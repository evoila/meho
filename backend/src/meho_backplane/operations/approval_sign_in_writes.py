# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Writes that can change the sign-in MEHO asks for before an approval.

An approve, and an admin's change of rights alone in the web console, need
a fresh sign-in, and the approval sign-in level where it is required
(:mod:`meho_backplane.auth.approval_sign_in`). Some writes of MEHO's own
login realm can change how that sign-in works: the clients MEHO uses for
it, or the token claims MEHO reads from it. A connector registers a check
for such an op with :func:`register_approval_sign_in_check`.

In MEHO's own login realm, a request for which the check passes
(:func:`changes_approval_sign_in`):

* never runs alone in the web console: it waits, also for a platform admin
  with a fresh sign-in
  (:func:`~meho_backplane.operations.grant_approval.may_make_alone`);
* is never approved by the login that asked, also not in the web console.
  A second platform admin approves it
  (:func:`~meho_backplane.operations.grant_approval.check_grant_approval`).

So one person alone cannot change the sign-in that guards the changes of
rights. The check reads the params and the approval card only (no I/O).
When the card does not hold what the check needs, the check passes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "approval_sign_in_check",
    "register_approval_sign_in_check",
]

#: op_id -> a check of a request: True when it can change the approval sign-in.
_CHECKS: dict[str, Callable[[ApprovalRequest], bool]] = {}


def register_approval_sign_in_check(op_id: str, check: Callable[[ApprovalRequest], bool]) -> None:
    """Record that a request of *op_id* can change the approval sign-in when *check* passes.

    *check* reads the request's params and its ``proposed_effect`` only.
    """
    _CHECKS[op_id] = check


def approval_sign_in_check(request: ApprovalRequest) -> bool:
    """True when the check registered for the op of *request* passes on it.

    ``False`` for an op with no check. The caller decides whether the
    request is in MEHO's own login realm.
    """
    check = _CHECKS.get(request.op_id)
    return check is not None and check(request)
