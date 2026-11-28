# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check that Keycloak answers for exactly the target's managed realm.

A Keycloak database can match a realm name without letter case or accents.
Then a request under ``/admin/realms/{realm}`` reaches another realm than the
one the target names. :class:`ManagedRealmChecks` reads the realm
(``GET /admin/realms/{realm}``) before the connector uses anything inside it
(users, clients, groups, roles, flows) and before it writes the realm
settings. The answered name must be exactly the managed realm
(:func:`~meho_backplane.connectors.keycloak.realm_params.check_answered_realm`),
else nothing more is sent.

Every admin login with at least one ``realm-management`` role in the realm
can read the name: without the right to view the realm, Keycloak answers with
a short form that has it. When Keycloak refuses the read with 403,
:func:`read_managed_realm` raises :class:`ManagedRealmReadRefusedError`. It
is still the HTTP 403 error, so the caller gets the usual
``connector_http_403`` result, and its ``error`` text starts with the fixed
sentence of this module: which read failed, and which right the login needs.

A passed check counts only inside one run of an operation
(:meth:`ManagedRealmChecks.run`, opened by
:func:`~meho_backplane.connectors.keycloak.answer_realm.name_the_realm`), and
there for at most :data:`CHECK_SECONDS`. So one operation does not read the
realm before each of its requests, and no call uses a check of an earlier
call. A request outside a run (the secret broker, an approval card, the check
before an approved run) reads the realm first, every time.

A read or a delete of the realm itself is not checked here: the caller checks
the answer it reads.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, Final

import httpx

from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.base import ConnectorOwnMessage
from meho_backplane.connectors.keycloak._paths import _ADMIN_REALM_PATH, fill_path
from meho_backplane.connectors.keycloak.realm_params import check_answered_realm
from meho_backplane.connectors.keycloak.session import resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "CHECK_SECONDS",
    "ManagedRealmChecks",
    "ManagedRealmReadRefusedError",
    "read_managed_realm",
    "realm_answer_unchecked",
    "unchecked_realm_answer",
]

#: How long one passed check counts inside one run.
CHECK_SECONDS: Final = 5.0

_REFUSED_TEXT: Final = (
    "Keycloak refused MEHO's read of the managed realm (HTTP 403). MEHO reads "
    "the realm before it uses or changes anything in it. The MEHO login needs at "
    "least one realm-management role in that realm, for example view-realm."
)


class ManagedRealmReadRefusedError(ConnectorOwnMessage, httpx.HTTPStatusError):
    """Keycloak answered 403 to the read of the managed realm.

    The message is the fixed sentence :data:`_REFUSED_TEXT`; it holds no
    vendor text and no value of the call. The 403 result takes the sentence
    from :attr:`OWN_TEXT`, never from the error's text. Request and response
    are the ones of the refused read, so the 403 result keeps its usual
    fields.
    """

    OWN_TEXT: ClassVar[str] = _REFUSED_TEXT

    def __init__(self, refused: httpx.HTTPStatusError) -> None:
        super().__init__(_REFUSED_TEXT, request=refused.request, response=refused.response)


@contextmanager
def _refusal_explained() -> Iterator[None]:
    """Raise :class:`ManagedRealmReadRefusedError` for a 403 of the realm read inside."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 403:
            raise
        raise ManagedRealmReadRefusedError(exc) from exc


async def read_managed_realm(
    connector: KeycloakConnector, target: KeycloakTargetLike, operator: Operator, path: str
) -> Any:
    """Read the realm at *path* (``GET /admin/realms/{realm}``) for an operation.

    For an operation that reads the managed realm itself before it changes
    it. Raises :class:`ManagedRealmReadRefusedError` when Keycloak answers 403.
    """
    with _refusal_explained():
        return await connector._get_admin_json(target, path, operator=operator)


_Passed = dict[tuple[tuple[str, str], str], float]

#: The passed checks of the current run; ``None`` outside a run.
_RUN_CHECKS: ContextVar[_Passed | None] = ContextVar("keycloak_realm_checks", default=None)


#: ``True`` while MEHO reads a realm whose name it has not checked yet.
_REALM_ANSWER_UNCHECKED: ContextVar[bool] = ContextVar(
    "keycloak_realm_answer_unchecked", default=False
)


def realm_answer_unchecked() -> bool:
    """True while MEHO reads a realm whose name it has not checked yet.

    The flight recorder stores no answer of that read: the realm it names is
    not known to be the managed realm until the name is checked.
    """
    return _REALM_ANSWER_UNCHECKED.get()


@contextmanager
def unchecked_realm_answer() -> Iterator[None]:
    """Mark the realm reads inside: the trace stores none of their answers.

    For a read of ``GET /admin/realms/{realm}`` whose answer MEHO checks only
    after it is read (:func:`.check_answered_realm`).
    """
    token = _REALM_ANSWER_UNCHECKED.set(True)
    try:
        yield
    finally:
        _REALM_ANSWER_UNCHECKED.reset(token)


def _realm_path(target: KeycloakTargetLike) -> str:
    return fill_path(_ADMIN_REALM_PATH, {"realm": resolve_realm_config(target).managed_realm})


async def _read_realm(
    connector: KeycloakConnector, target: KeycloakTargetLike, operator: Operator, path: str
) -> Any:
    """Keycloak's answer for the realm."""
    with unchecked_realm_answer():
        return await connector._get_json(target, path, operator=operator)


class ManagedRealmChecks:
    """The passed checks of the current run, per target and managed realm."""

    @contextmanager
    def run(self) -> Iterator[None]:
        """One run of an operation: checks passed in it count only until it ends."""
        token = _RUN_CHECKS.set({})
        try:
            yield
        finally:
            _RUN_CHECKS.reset(token)

    def forget(self, target: KeycloakTargetLike) -> None:
        """Drop every passed check of *target*; the next request checks again."""
        passed = _RUN_CHECKS.get()
        if passed is None:
            return
        key = target_cache_key(target)
        for entry in [entry for entry in passed if entry[0] == key]:
            del passed[entry]

    def clear(self) -> None:
        """Drop every passed check."""
        passed = _RUN_CHECKS.get()
        if passed is not None:
            passed.clear()

    async def ensure(
        self,
        connector: KeycloakConnector,
        target: KeycloakTargetLike,
        operator: Operator,
        method: str,
        path: str,
    ) -> None:
        """Check the realm before *method* *path*, when the request uses the managed realm."""
        realm_path = _realm_path(target)
        if path.startswith(f"{realm_path}/") or (method == "PUT" and path == realm_path):
            await self.check(connector, target, operator)

    async def check(
        self, connector: KeycloakConnector, target: KeycloakTargetLike, operator: Operator
    ) -> None:
        """Check the realm, unless a check passed in this run in the last :data:`CHECK_SECONDS`.

        Raises :class:`ValueError` when Keycloak answers with another realm
        name, with no name, or with something that is not an object. Raises
        :class:`ManagedRealmReadRefusedError` when Keycloak answers 403.
        """
        managed = resolve_realm_config(target).managed_realm
        key = (target_cache_key(target), managed)
        passed = _RUN_CHECKS.get()
        if passed is not None and passed.get(key, 0.0) > time.monotonic():
            return
        with _refusal_explained():
            answer = await _read_realm(connector, target, operator, _realm_path(target))
        check_answered_realm("keycloak", answer, target)
        if passed is not None:
            passed[key] = time.monotonic() + CHECK_SECONDS
