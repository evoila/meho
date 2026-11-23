# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The sign-in a console approve needs, and where the console sends a person for it (#6282).

``APPROVAL_SIGN_IN_LEVEL`` decides, for the console's approval client
(:attr:`~meho_backplane.settings.Settings.ui_keycloak_approve_client_id`):

* ``required``: an approve needs the console's approval sign-in with the
  approval level. The sign-in link is ``/ui/auth/approval-sign-in``.
* ``observe`` and ``off``: the console approves as before. The sign-in
  link is the new console login with ``prompt=login``; ``observe``
  records the level it saw.

``auto`` is resolved as on the command line, from the binding of the
console's approval client
(:func:`~meho_backplane.auth.approval_sign_in_flow.read_web_approval_sign_in`).
An empty console approval client setting makes ``auto`` record only.
"""

from __future__ import annotations

import time
import uuid

from fastapi import HTTPException

from meho_backplane.auth.approval_sign_in import ApprovalAuth, approval_sign_in_max_age
from meho_backplane.auth.approval_sign_in_flow import SignInLevel, sign_in_level_in_force
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.web_approval_sign_in import (
    check_web_approval_sign_in,
    web_approval_sign_in_ready,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.approval_sign_in_routes import approval_sign_in_location
from meho_backplane.ui.auth.approval_token_store import approval_operator
from meho_backplane.ui.auth.routes import login_location

__all__ = [
    "approve_sign_in_state",
    "console_run_auth",
    "sign_in_url",
    "web_approval_auth",
    "web_sign_in_level",
]


async def web_sign_in_level() -> SignInLevel:
    """What MEHO does now with a console approve that lacks the approval level.

    An empty console approval client setting counts as a missing client:
    ``auto`` then only records, because the console cannot start an
    approval sign-in. An explicit ``required`` still refuses.
    """
    return await sign_in_level_in_force(get_settings(), web=True)


def sign_in_url(return_to: str, level: SignInLevel) -> str:
    """Where the console sends a person whose sign-in cannot approve."""
    if level.in_force == "required":
        return approval_sign_in_location(return_to)
    return login_location(return_to, fresh_sign_in=True)


async def web_approval_auth(operator: Operator, session_id: uuid.UUID) -> ApprovalAuth:
    """The proof for a console approve by *operator*, or the console's ``401``."""
    level = await web_sign_in_level()
    return check_web_approval_sign_in(
        operator,
        await approval_operator(session_id),
        get_settings(),
        level_in_force=level.in_force,
        level_check_failed=level.check_failed,
    )


async def console_run_auth(operator: Operator) -> ApprovalAuth | None:
    """The proof for a direct run of a change of rights in the console, or ``None``.

    The rule of a console approve of such a change: the proof of
    :func:`web_approval_auth`, and a sign-in no older than the approval
    window (``FRESH_SIGN_IN_MAX_AGE_SECONDS``), the limit
    :func:`~meho_backplane.operations.approval_queue.approve_request`
    applies to a request that is not ``destructive``. ``None`` for a call
    that did not come from a console session.
    """
    if not operator.web_session:
        return None
    session_id = operator.web_session_id
    level = await web_sign_in_level()
    settings = get_settings()
    try:
        auth = check_web_approval_sign_in(
            operator,
            await approval_operator(session_id) if session_id is not None else None,
            settings,
            level_in_force=level.in_force,
            level_check_failed=level.check_failed,
        )
    except HTTPException:
        return None
    if time.time() - auth.auth_time > approval_sign_in_max_age(settings, destructive=False):
        return None
    return auth


async def approve_sign_in_state(
    operator: Operator, session_id: uuid.UUID, return_to: str
) -> tuple[bool, str]:
    """``(needs a sign-in, the sign-in link)`` for the Approve button of *operator*."""
    level = await web_sign_in_level()
    ready = web_approval_sign_in_ready(
        operator,
        await approval_operator(session_id),
        get_settings(),
        level_in_force=level.in_force,
    )
    return not ready, sign_in_url(return_to, level)
