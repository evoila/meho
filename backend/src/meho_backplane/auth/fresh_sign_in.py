# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The fresh sign-in check: proof that a person just entered their credentials.

Some actions must come from a person, not from a token that an agent or a
script could hold: turning approval off for an operation (#4711) and, with
#3979, approving a waiting change on the CLI or REST. The proof is the
token's ``auth_time`` claim (OIDC Core section 2): the time of the last
sign-in. A token refresh keeps that time (RFC 9470 section 6.1), so an old
sign-in stays old however often the token is renewed.

:func:`has_fresh_sign_in` passes only for a person (not an agent, a service
account or a runner) whose ``auth_time`` is at most *max_age_seconds* old.
There are two limits (#5515):

* ``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS`` (default 300) for an action that
  needs a fresh sign-in each time: turning approval off, and approving a
  ``destructive`` operation. It is the default here.
* ``FRESH_SIGN_IN_MAX_AGE_SECONDS`` (default 900), the approval window: one
  sign-in may approve several other requests while it is this young.

A token without ``auth_time`` fails (fail closed).
:func:`fresh_sign_in_challenge` builds the ``401`` answer from RFC 9470
section 3, so a client knows to sign in again.

The console does not use this check: a click in the web session is the
person's proof there.
"""

from __future__ import annotations

import time
from typing import Final

from fastapi import HTTPException, status

from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.settings import get_settings

__all__ = ["CLOCK_SKEW_SECONDS", "fresh_sign_in_challenge", "has_fresh_sign_in"]

#: How far in the future an ``auth_time`` may be, for clock drift between
#: Keycloak and MEHO.
CLOCK_SKEW_SECONDS: Final[int] = 60


def has_fresh_sign_in(
    operator: Operator, *, now: float | None = None, max_age_seconds: int | None = None
) -> bool:
    """Return ``True`` when *operator* is a person who signed in just now.

    *max_age_seconds* defaults to ``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS``.
    """
    if not is_human_principal(operator) or operator.auth_time is None:
        return False
    if max_age_seconds is None:
        max_age_seconds = get_settings().per_action_sign_in_max_age_seconds
    age = (time.time() if now is None else now) - operator.auth_time
    return -CLOCK_SKEW_SECONDS <= age <= max_age_seconds


def fresh_sign_in_challenge(detail: str, *, max_age_seconds: int | None = None) -> HTTPException:
    """Return the ``401`` that asks the client for a fresh sign-in (RFC 9470).

    *max_age_seconds* defaults to ``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS``.
    """
    if max_age_seconds is None:
        max_age_seconds = get_settings().per_action_sign_in_max_age_seconds
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={
            "WWW-Authenticate": (
                f'Bearer error="insufficient_user_authentication", max_age={max_age_seconds}'
            )
        },
    )
