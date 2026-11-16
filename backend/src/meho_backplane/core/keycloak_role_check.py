# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Before an upgrade: does ``meho-admin`` have ``manage-realm``? (#6336)

The ``bootstrap`` Job runs after an upgrade and stops when ``meho-admin``
lacks the realm-management role ``manage-realm`` (#6280). By then the new
version already runs. So the chart runs this check as a pre-upgrade hook,
before the upgrade changes anything. When the role is missing, the check
fails, and the upgrade stops with the old version still running.

The check reads the effective client roles of ``meho-admin``'s service
account. ``meho-admin`` may read them with ``manage-clients`` and
``manage-users``, which every realm file gave it. Only a clear "no" stops
the upgrade. When the check cannot tell (Keycloak does not answer, the
login is refused, a read fails), it says so in one line and lets the
upgrade go on: the ``bootstrap`` Job checks again after the upgrade.

The output names no secret and no token. Details:
docs/codebase/core-bootstrap.md, "The role check before an upgrade".
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from meho_backplane.core.keycloak import ADMIN_CLIENT_ID, ADMIN_SECRET_ENV, load_plan
from meho_backplane.core.keycloak_api import REALM, Admin, HttpAdmin, RealmStageError

__all__ = ["MISSING_ROLE", "ROLE", "check", "run"]

ROLE = "manage-realm"
_MANAGEMENT_CLIENT = "realm-management"
_HTTP_TIMEOUT_S = 10.0

#: Why the upgrade stops. Helm shows the Job name; the Job log shows this.
MISSING_ROLE = (
    f"{ADMIN_CLIENT_ID} lacks the {_MANAGEMENT_CLIENT} role {ROLE}, so the bootstrap Job "
    "would stop after the upgrade. The upgrade stops now and changes nothing. Give "
    f"{ADMIN_CLIENT_ID} the role {ROLE} once, then run the upgrade again; see "
    "docs-site/install/upgrades.md, 'The bootstrap Job needs manage-realm'"
)


class _UnknownError(Exception):
    """The check cannot tell whether the role is there."""


def _say(line: str) -> None:
    print(line, flush=True)


async def _get(
    admin: Admin, path: str, what: str, params: Mapping[str, str] | None = None
) -> object:
    try:
        reply = await admin.call("GET", path, params=params)
    except RealmStageError as exc:
        raise _UnknownError(f"{what}: {exc}") from None
    if reply.status != 200:
        raise _UnknownError(f"{what}: HTTP {reply.status}")
    return reply.data


async def _client_uuid(admin: Admin, client_id: str) -> str:
    what = f"cannot look up client {client_id}"
    found = await _get(admin, f"/admin/realms/{REALM}/clients", what, {"clientId": client_id})
    if isinstance(found, list):
        for client in found:
            if (
                isinstance(client, dict)
                and client.get("clientId") == client_id
                and client.get("id")
            ):
                return str(client["id"])
    raise _UnknownError(f"{what}: not found")


async def _has_role(admin: Admin) -> bool:
    admin_uuid = await _client_uuid(admin, ADMIN_CLIENT_ID)
    account = await _get(
        admin,
        f"/admin/realms/{REALM}/clients/{admin_uuid}/service-account-user",
        f"cannot read the service account of {ADMIN_CLIENT_ID}",
    )
    if not isinstance(account, dict) or not account.get("id"):
        raise _UnknownError(f"cannot read the service account of {ADMIN_CLIENT_ID}: no id")
    management = await _client_uuid(admin, _MANAGEMENT_CLIENT)
    # ``composite``: the effective roles, so a composite such as realm-admin counts.
    roles = await _get(
        admin,
        f"/admin/realms/{REALM}/users/{account['id']}/role-mappings/clients/{management}/composite",
        f"cannot read the roles of {ADMIN_CLIENT_ID}",
    )
    if not isinstance(roles, list):
        raise _UnknownError(f"cannot read the roles of {ADMIN_CLIENT_ID}: the answer is not a list")
    return any(isinstance(role, dict) and role.get("name") == ROLE for role in roles)


async def _probe(admin: Admin, secret: str) -> bool:
    try:
        ready = await admin.realm_ready()
    except RealmStageError as exc:
        raise _UnknownError(str(exc)) from None
    if not ready:
        raise _UnknownError(f"Keycloak does not serve realm {REALM}")
    try:
        logged_in = await admin.login(REALM, ADMIN_CLIENT_ID, secret)
    except RealmStageError as exc:
        raise _UnknownError(str(exc)) from None
    if not logged_in:
        raise _UnknownError(f"Keycloak refused the login of {ADMIN_CLIENT_ID}")
    return await _has_role(admin)


async def check(admin: Admin, environ: Mapping[str, str]) -> None:
    """Raise :class:`RealmStageError` with :data:`MISSING_ROLE` when the role is missing."""
    secret = environ.get(ADMIN_SECRET_ENV, "")
    try:
        if not secret:
            raise _UnknownError(f"no secret for {ADMIN_CLIENT_ID} yet")
        has_role = await _probe(admin, secret)
    except _UnknownError as exc:
        _say(
            f"role check: unknown ({exc}); the upgrade goes on, "
            "and the bootstrap Job checks again after it"
        )
        return
    if not has_role:
        raise RealmStageError(MISSING_ROLE)
    _say(f"role check: {ADMIN_CLIENT_ID} has {ROLE}")


async def run(environ: Mapping[str, str] | None = None) -> None:
    """The ``keycloak-role-check`` stage of ``python -m meho_backplane.core.bootstrap``."""
    env = dict(os.environ if environ is None else environ)
    plan = load_plan(env)
    async with httpx.AsyncClient(base_url=plan.keycloak_url, timeout=_HTTP_TIMEOUT_S) as http:
        await check(HttpAdmin(http), env)
