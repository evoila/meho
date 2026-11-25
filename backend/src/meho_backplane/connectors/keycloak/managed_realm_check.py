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

Every admin login can read the name: without the right to view the realm,
Keycloak answers with a short form that has it.

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
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.keycloak._paths import _ADMIN_REALM_PATH, fill_path
from meho_backplane.connectors.keycloak.realm_params import check_answered_realm
from meho_backplane.connectors.keycloak.session import resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["CHECK_SECONDS", "ManagedRealmChecks"]

#: How long one passed check counts inside one run.
CHECK_SECONDS: Final = 5.0

_Passed = dict[tuple[tuple[str, str], str], float]

#: The passed checks of the current run; ``None`` outside a run.
_RUN_CHECKS: ContextVar[_Passed | None] = ContextVar("keycloak_realm_checks", default=None)


def _realm_path(target: KeycloakTargetLike) -> str:
    return fill_path(_ADMIN_REALM_PATH, {"realm": resolve_realm_config(target).managed_realm})


async def _read_realm(
    connector: KeycloakConnector, target: KeycloakTargetLike, operator: Operator, path: str
) -> Any:
    """Keycloak's answer for the realm."""
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
        name, with no name, or with something that is not an object.
        """
        managed = resolve_realm_config(target).managed_realm
        key = (target_cache_key(target), managed)
        passed = _RUN_CHECKS.get()
        if passed is not None and passed.get(key, 0.0) > time.monotonic():
            return
        answer = await _read_realm(connector, target, operator, _realm_path(target))
        check_answered_realm("keycloak", answer, target)
        if passed is not None:
            passed[key] = time.monotonic() + CHECK_SECONDS
