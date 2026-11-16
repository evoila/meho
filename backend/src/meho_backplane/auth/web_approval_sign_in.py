# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console approval needs the same sign-in level as the command line (#6282).

The console has its own approval client
(:attr:`~meho_backplane.settings.Settings.ui_keycloak_approve_client_id`,
default ``meho-web-approve``). Keycloak binds it to the approval sign-in
flow ``meho approval sign-in level``, the flow of the command line's
approval client. So a sign-in with it gets the approval level
``meho-approve`` only from that flow's own steps: MEHO's password form and
the one-time code. A sign-in through the company login does not get it.
The console's normal login does not use this client and never carries the
level.

The console keeps the token of the approval sign-in on the session row
(:mod:`meho_backplane.ui.auth.approval_sign_in_routes`). An approve uses
it as the proof (:func:`check_web_approval_sign_in`):

* ``required``: only that token approves. It must belong to the session's
  person, come from the console's approval client, have asked for
  credentials, be recent, and carry the approval level (or a higher one).
* ``observe`` and ``off``: the console approves as before, on that token
  when it passes the other checks, else on the session's own recent
  sign-in. ``observe`` logs and records a missing level.

The approval row records the channel (``web``), the ``acr`` the proof had,
``level_in_force`` and, under ``observe``, ``level_missing``.
"""

from __future__ import annotations

from typing import Any

import structlog

from meho_backplane.auth.approval_sign_in import (
    WEB_FRESH_SIGN_IN_REMEDIATION,
    ApprovalAuth,
    _approval_auth,
    _level_remedy,
    _longest_sign_in_max_age,
    asked_for_credentials,
    check_fresh_web_approval_sign_in,
    has_approval_level,
    has_fresh_web_approval_sign_in,
)
from meho_backplane.auth.fresh_sign_in import fresh_sign_in_challenge, has_fresh_sign_in
from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.settings import Settings

__all__ = [
    "check_web_approval_sign_in",
    "is_web_approval_sign_in",
    "web_approval_sign_in_ready",
]


def is_web_approval_sign_in(operator: Operator, settings: Settings) -> bool:
    """Whether the token comes from a person's console approval sign-in.

    True only for a person whose token's ``azp`` claim is the console's
    approval client. An empty client setting means no token is one.
    """
    client_id = settings.ui_keycloak_approve_client_id
    return bool(client_id) and is_human_principal(operator) and operator.azp == client_id


def _mode(level_in_force: str) -> str:
    """``off``, ``observe`` or ``required``; any other value counts as ``required``."""
    return level_in_force if level_in_force in ("off", "observe") else "required"


def _usable(
    session_operator: Operator,
    approval_operator: Operator | None,
    settings: Settings,
    now: float | None,
) -> bool:
    """Whether *approval_operator* passes every check of a console approve but the level."""
    return (
        approval_operator is not None
        and approval_operator.auth_time is not None
        and approval_operator.sub == session_operator.sub
        and is_web_approval_sign_in(approval_operator, settings)
        and asked_for_credentials(approval_operator)
        and has_fresh_sign_in(
            approval_operator, now=now, max_age_seconds=_longest_sign_in_max_age(settings)
        )
    )


def web_approval_sign_in_ready(
    session_operator: Operator,
    approval_operator: Operator | None,
    settings: Settings,
    *,
    level_in_force: str,
    now: float | None = None,
) -> bool:
    """Whether the console may show the Approve button. It logs nothing.

    The same rule as :func:`check_web_approval_sign_in`. *approval_operator*
    is the person's console approval sign-in, or ``None`` when the session
    has none.
    """
    required = _mode(level_in_force) == "required"
    if _usable(session_operator, approval_operator, settings, now):
        assert approval_operator is not None
        if not required or has_approval_level(approval_operator):
            return True
    if required:
        return False
    return has_fresh_web_approval_sign_in(session_operator, settings, now=now)


def check_web_approval_sign_in(
    session_operator: Operator,
    approval_operator: Operator | None,
    settings: Settings,
    *,
    level_in_force: str,
    level_check_failed: bool = False,
    now: float | None = None,
) -> ApprovalAuth:
    """Return the proof for an approve in the web interface, or raise the ``401``.

    *approval_operator* is the token of the person's console approval
    sign-in, kept on the session row, or ``None``. *level_in_force* is
    ``off``, ``observe`` or ``required``, as the route resolved
    ``APPROVAL_SIGN_IN_LEVEL`` for the console's approval client.

    Under ``required`` only the approval sign-in token approves, and it
    must carry the approval level. The refusal is the ``401`` with the
    console's text. When the level is the only missing part, it adds
    :data:`~meho_backplane.auth.approval_sign_in.LEVEL_MISSING_REMEDIATION`
    (or ``LEVEL_UNCHECKED_REMEDIATION`` when *level_check_failed*). Under
    ``observe`` and ``off`` the session's own sign-in approves as before
    (:func:`~meho_backplane.auth.approval_sign_in.check_fresh_web_approval_sign_in`)
    when there is no usable approval sign-in token.
    """
    mode = _mode(level_in_force)
    log = structlog.get_logger(__name__)
    if _usable(session_operator, approval_operator, settings, now):
        assert approval_operator is not None
        level = has_approval_level(approval_operator)
        if mode != "required" or level:
            return _proof(approval_operator, settings, mode, level, log)
        log.warning(
            "approval_sign_in_not_fresh",
            operator_sub=session_operator.sub,
            approval_client=True,
            approval_level=False,
        )
        raise fresh_sign_in_challenge(
            f"fresh_sign_in_required: {WEB_FRESH_SIGN_IN_REMEDIATION} "
            f"{_level_remedy(level_check_failed)}",
            max_age_seconds=settings.fresh_sign_in_max_age_seconds,
        )
    if mode == "required":
        log.warning(
            "approval_sign_in_not_fresh",
            operator_sub=session_operator.sub,
            approval_client=approval_operator is not None,
            approval_level=False,
        )
        raise fresh_sign_in_challenge(
            f"fresh_sign_in_required: {WEB_FRESH_SIGN_IN_REMEDIATION}",
            max_age_seconds=settings.fresh_sign_in_max_age_seconds,
        )
    # Raises the console's 401 when the session's own sign-in is too old.
    check_fresh_web_approval_sign_in(session_operator, settings, now=now)
    return _proof(session_operator, settings, mode, has_approval_level(session_operator), log)


def _proof(
    operator: Operator, settings: Settings, mode: str, level: bool, log: Any
) -> ApprovalAuth:
    """The console proof from *operator*'s sign-in, with the level facts."""
    level_missing = mode == "observe" and not level
    if level_missing:
        log.warning("approval_sign_in_level_missing", operator_sub=operator.sub, acr=operator.acr)
    assert operator.auth_time is not None
    return _approval_auth(
        operator,
        settings,
        channel="web",
        auth_time=operator.auth_time,
        level_missing=level_missing,
        level_in_force=mode,
    )
