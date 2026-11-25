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

The check runs twice for a call:

* :func:`realm_gate_check` runs before the policy gate (the dispatcher and
  ``preview_operation``, see
  :func:`~meho_backplane.operations._park_check.register_gate_check`). So it
  runs before the reads that decide who must approve and before any approval
  card or preview reads Keycloak. A refusal there stores no approval request.
  It runs only after the caller's rights let the call go on, so a call the
  caller may not make reads nothing of Keycloak.
* :func:`realm_checked` wraps the handler method of every operation, so the
  check runs again when the call runs, also after an approval.

Both first check the realm a call names in its params
(:func:`~meho_backplane.connectors.keycloak.realm_params.realm_fence_problem`).
A realm other than the managed realm is refused before any request.

A read inside an approval card that gets another realm's answer refuses the
call too (:func:`~meho_backplane.connectors.keycloak.realm_params.check_answered_realm`).

When the check read gets no answer at all (for example the TLS certificate
of the server is not trusted), the call stops there with the error it would
return for it (for example ``connector_tls_verify_failed``,
:func:`~meho_backplane.operations._park_check.gate_check_refusal`). So no
call waits for approval, and no preview says it may go on, without a
passed check.

``keycloak.realm.create`` and ``keycloak.realm.delete`` use
:func:`realm_checked_or_absent` and :data:`ABSENT_OK_OPS`: they also go on
when the managed realm does not exist yet (the read answers HTTP 404), so a
first setup can make that realm and a teardown can run twice. Any other
answer gets the same check.

The flight recorder records no span for the check read, so a stored trace
holds only the calls of the operation itself and no value of the realm that
Keycloak answered with. A passed check also counts for the realm check
before each request of the same run
(:meth:`~meho_backplane.connectors.keycloak.managed_realm_check.ManagedRealmChecks.note_passed`).
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.keycloak._paths import _ADMIN_REALM_PATH, fill_path
from meho_backplane.connectors.keycloak.realm_params import realm_fence_problem
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.operations._park_check import ParkCheck, register_gate_check

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "ABSENT_OK_OPS",
    "KeycloakRealmAnswerError",
    "check_realm_answer",
    "realm_checked",
    "realm_checked_or_absent",
    "realm_gate_check",
    "register_realm_gate_checks",
]

#: The operations that also go on when the managed realm does not exist (HTTP 404).
ABSENT_OK_OPS: Final = frozenset({"keycloak.realm.create", "keycloak.realm.delete"})


class KeycloakRealmAnswerError(ValueError):
    """Keycloak did not answer with the realm that the target manages."""


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
    try:
        with flight_recorder_capture.paused():
            answer: Any = await connector._get_admin_json(
                target, fill_path(_ADMIN_REALM_PATH, {"realm": managed}), operator=operator
            )
    except httpx.HTTPStatusError as exc:
        if allow_absent and exc.response.status_code == 404:
            return
        raise
    name = answer.get("realm") if isinstance(answer, dict) else None
    if name != managed:
        raise KeycloakRealmAnswerError(
            f"target {getattr(target, 'name', None)!r}: Keycloak did not answer with the "
            "realm this target manages. Check the 'managed_realm' of the target and the "
            "address of the Keycloak server."
        )
    connector._realm_checks.note_passed(target)


def realm_gate_check(op_id: str) -> ParkCheck:
    """The check of *op_id* before the policy gate: the refusal text, or ``None``.

    The text is the one the handler gives. A target or connector that does
    not resolve is left to the dispatcher, which refuses the call itself.
    """

    async def check(ctx: PreviewContext) -> str | None:
        # Imported here: the connector module imports this one.
        from meho_backplane.connectors.keycloak.connector import KeycloakConnector

        connector = ctx.connector_instance
        if not isinstance(connector, KeycloakConnector) or ctx.target is None:
            return None
        problem = realm_fence_problem(op_id, ctx.params, ctx.target)
        if problem is not None:
            return problem
        try:
            await check_realm_answer(
                connector, ctx.operator, ctx.target, allow_absent=op_id in ABSENT_OK_OPS
            )
        except KeycloakRealmAnswerError as exc:
            return str(exc)
        return None

    return check


def register_realm_gate_checks(op_ids: Iterable[str]) -> None:
    """Run :func:`realm_gate_check` before the policy gate of each of *op_ids*."""
    for op_id in op_ids:
        register_gate_check(op_id, realm_gate_check(op_id))


@functools.cache
def _op_ids_by_handler() -> dict[str, str]:
    """``handler_attr`` -> ``op_id`` of every Keycloak operation."""
    # Imported here: the connector module imports this one.
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    return {op.handler_attr: op.op_id for op in KeycloakConnector.operation_specs()}


def _checked[R](
    handler: Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]],
    *,
    allow_absent: bool,
) -> Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]]:
    @functools.wraps(handler)
    async def _wrapped(self: Any, operator: Operator, target: Any, params: dict[str, Any]) -> R:
        op_id = _op_ids_by_handler().get(handler.__name__)
        problem = realm_fence_problem(op_id, params, target) if op_id is not None else None
        if problem is not None:
            raise ValueError(problem)
        await check_realm_answer(self, operator, target, allow_absent=allow_absent)
        return await handler(self, operator, target, params)

    _wrapped.realm_answer_checked = True  # type: ignore[attr-defined]
    return _wrapped


def realm_checked[R](
    handler: Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]],
) -> Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]]:
    """Wrap an operation handler method so it runs only after :func:`check_realm_answer`.

    A realm the call names in its params that is not the managed realm is
    refused first, with no request.
    """
    return _checked(handler, allow_absent=False)


def realm_checked_or_absent[R](
    handler: Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]],
) -> Callable[[Any, Operator, Any, dict[str, Any]], Awaitable[R]]:
    """Like :func:`realm_checked`, but a managed realm that does not exist (404) passes."""
    return _checked(handler, allow_absent=True)
