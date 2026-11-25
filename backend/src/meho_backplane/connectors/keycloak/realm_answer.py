# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Keycloak operation checks which realm Keycloak answers with.

Before an operation reads or changes realm data, MEHO reads the target's
managed realm (``GET /admin/realms/{realm}``) and compares the ``realm``
field of the answer with the managed realm of the target. The two names must
be exactly the same, with the same letter case and no space around them.
When they differ, or the answer has no realm name, the operation returns an
error and sends nothing more to Keycloak. The error names the target and
says what to check. It holds no value from the answer.

:func:`realm_checked` wraps the handler method of an operation. It runs
after the params check and the policy gate, in the same place as the
handler itself, so it applies to every call that runs the handler.
``keycloak.user.check_login`` is not wrapped: it sends no admin request.

:func:`realm_checked_or_absent` wraps ``keycloak.realm.create`` and
``keycloak.realm.delete``. They also run when the managed realm does not
exist yet (the read answers HTTP 404), so a first setup can make that realm
and a teardown can run twice. Any other answer gets the same check.

The flight recorder stores the check read with a fixed text in place of the
answer (:data:`REALM_CHECK_TRACE_BODY`), so a stored trace holds no value of
the realm that Keycloak answered with.
"""

from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.keycloak._paths import _ADMIN_REALM_PATH, fill_path
from meho_backplane.connectors.keycloak.session import resolve_realm_config

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "REALM_CHECK_TRACE_BODY",
    "KeycloakRealmAnswerError",
    "check_realm_answer",
    "realm_check_read_active",
    "realm_checked",
    "realm_checked_or_absent",
]


#: What the flight recorder stores in place of the answer of the check read.
REALM_CHECK_TRACE_BODY: Final[dict[str, str]] = {
    "left_out": "MEHO read this answer only to check the realm name."
}

_realm_check_read: ContextVar[bool] = ContextVar("keycloak_realm_check_read", default=False)


class KeycloakRealmAnswerError(ValueError):
    """Keycloak did not answer with the realm that the target manages."""


def realm_check_read_active() -> bool:
    """Return ``True`` while :func:`check_realm_answer` reads the managed realm."""
    return _realm_check_read.get()


async def check_realm_answer(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    *,
    allow_absent: bool = False,
) -> None:
    """Raise :class:`KeycloakRealmAnswerError` unless Keycloak answers with the managed realm.

    With *allow_absent*, an HTTP 404 for the managed realm passes the check.
    """
    managed = resolve_realm_config(target).managed_realm
    token = _realm_check_read.set(True)
    try:
        answer: Any = await connector._get_admin_json(
            target, fill_path(_ADMIN_REALM_PATH, {"realm": managed}), operator=operator
        )
    except httpx.HTTPStatusError as exc:
        if allow_absent and exc.response.status_code == 404:
            return
        raise
    finally:
        _realm_check_read.reset(token)
    name = answer.get("realm") if isinstance(answer, dict) else None
    if name != managed:
        raise KeycloakRealmAnswerError(
            f"target {getattr(target, 'name', None)!r}: Keycloak did not answer with the "
            "realm this target manages. Check the 'managed_realm' of the target and the "
            "address of the Keycloak server."
        )


def realm_checked[R](
    handler: Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]],
) -> Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]]:
    """Wrap an operation handler method so it runs only after :func:`check_realm_answer`."""

    @functools.wraps(handler)
    async def _wrapped(self: Any, operator: Operator, target: Any, params: dict[str, Any]) -> R:
        await check_realm_answer(self, operator, target)
        return await handler(self, operator, target, params)

    return _wrapped


def realm_checked_or_absent[R](
    handler: Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]],
) -> Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]]:
    """Like :func:`realm_checked`, but a managed realm that does not exist (404) passes."""

    @functools.wraps(handler)
    async def _wrapped(self: Any, operator: Operator, target: Any, params: dict[str, Any]) -> R:
        await check_realm_answer(self, operator, target, allow_absent=True)
        return await handler(self, operator, target, params)

    return _wrapped
