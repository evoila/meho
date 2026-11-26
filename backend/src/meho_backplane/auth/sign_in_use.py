# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One fresh sign-in, one use: what a sign-in has already been used for.

A fresh sign-in is proof that a person just entered their credentials. Two
paths spend it:

* an approve of a waiting request (``approval.decision`` audit row, the
  sign-in is in ``payload["approval_auth"]``);
* the removal of the person's own block (``principal.unblock`` audit row,
  the sign-in is in ``payload["sign_in"]``).

Both rows keep the same ``sign_in_key``: a hash of ``sub``, ``sid`` and
``auth_time``
(:meth:`~meho_backplane.auth.approval_sign_in.ApprovalAuth.sign_in_key`).
:func:`earlier_sign_in_uses` reads the person's earlier rows of both kinds,
so a new path that spends a sign-in needs to be added in one place.

The caller takes the advisory lock of the sign-in first
(:func:`~meho_backplane.operations.approval_queue.take_approval_lock`, scope
``approval-sign-in``), so two uses of one sign-in at the same moment run one
after the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.fresh_sign_in import CLOCK_SKEW_SECONDS
from meho_backplane.db.models import AuditLog

__all__ = ["SIGN_IN_LOCK_SCOPE", "SignInUse", "earlier_sign_in_uses"]

#: The scope of the advisory lock that orders two uses of one sign-in.
SIGN_IN_LOCK_SCOPE: Final[str] = "approval-sign-in"

_APPROVAL_PATH: Final[str] = "approval.decision"
_UNBLOCK_OP_ID: Final[str] = "principal.unblock"
_UNBLOCK_PATH_PREFIX: Final[str] = "/api/v1/principals/blocked/"


@dataclass(frozen=True)
class SignInUse:
    """One earlier use of a sign-in.

    *kind* is ``approval`` or ``unblock``. *single_use* is ``True`` for an
    approval that used the sign-in alone (a ``destructive`` request).
    """

    kind: Literal["approval", "unblock"]
    single_use: bool = False


def _use_of(path: str, payload: Any, sign_in_key: str) -> SignInUse | None:
    """The use a row records for *sign_in_key*, or ``None``."""
    if not isinstance(payload, dict):
        return None
    if path == _APPROVAL_PATH:
        earlier = payload.get("approval_auth")
        if isinstance(earlier, dict) and earlier.get("sign_in_key") == sign_in_key:
            return SignInUse("approval", single_use=earlier.get("single_use") is True)
        return None
    earlier = payload.get("sign_in")
    if (
        payload.get("op_id") == _UNBLOCK_OP_ID
        and isinstance(earlier, dict)
        and earlier.get("sign_in_key") == sign_in_key
    ):
        return SignInUse("unblock")
    return None


async def earlier_sign_in_uses(
    session: AsyncSession, *, sub: str, sign_in_key: str, auth_time: int
) -> list[SignInUse]:
    """Return the earlier uses of the sign-in *sign_in_key* of person *sub*.

    The scan reads the person's audit rows written since the sign-in. It
    starts :data:`~meho_backplane.auth.fresh_sign_in.CLOCK_SKEW_SECONDS`
    before ``auth_time``: the fresh sign-in check accepts an ``auth_time``
    that far in the future, so an earlier use can have an audit time before
    ``auth_time``. The scan is short: the sign-in is minutes old.
    """
    since = datetime.fromtimestamp(auth_time - CLOCK_SKEW_SECONDS - 1, UTC)
    rows = await session.execute(
        select(AuditLog.path, AuditLog.payload).where(
            AuditLog.operator_sub == sub,
            AuditLog.occurred_at >= since,
            or_(
                AuditLog.path == _APPROVAL_PATH,
                AuditLog.path.startswith(_UNBLOCK_PATH_PREFIX),
            ),
        )
    )
    uses = (_use_of(path, payload, sign_in_key) for path, payload in rows)
    return [use for use in uses if use is not None]
