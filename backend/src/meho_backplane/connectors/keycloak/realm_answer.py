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
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import _ADMIN_REALM_PATH, fill_path
from meho_backplane.connectors.keycloak.session import resolve_realm_config

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["KeycloakRealmAnswerError", "check_realm_answer", "realm_checked"]


class KeycloakRealmAnswerError(ValueError):
    """Keycloak did not answer with the realm that the target manages."""


async def check_realm_answer(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
) -> None:
    """Raise :class:`KeycloakRealmAnswerError` unless Keycloak answers with the managed realm."""
    managed = resolve_realm_config(target).managed_realm
    answer: Any = await connector._get_admin_json(
        target, fill_path(_ADMIN_REALM_PATH, {"realm": managed}), operator=operator
    )
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
