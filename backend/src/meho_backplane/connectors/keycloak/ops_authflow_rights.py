# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login flow ops check the rights of the target's account before they wait.

The ``keycloak.authflow.ensure_*`` ops change login flows. Keycloak lets an
account do that only with the realm-management role ``manage-realm``. An
account without it passes the preview, a person approves, and the run fails
with HTTP 403 at its first write. So each of these ops registers, with
:func:`register_rights_check`:

* a check before the wait (``register_permission_preflight``). It reads the
  roles of the target's account. When a role the run needs is missing, it
  sets ``will_be_denied``, so the dispatcher refuses the call before an
  approval request exists. The text names the missing role and the account;
* the approval card (``register_preview_builder``). It lists the roles the
  run needs, and says in one line whether MEHO could check them;
* :func:`names_missing_rights` on the handler: a 403 at the run still stops
  with the same text.

Where the roles come from. Keycloak decides the rights of an Admin REST API
call from the roles in the caller's access token (``MgmtPermissions`` builds
a ``KeycloakIdentity`` from the token; ``hasOneAdminRole`` reads the client
roles of the admin client). The admin client is ``realm-management`` when
the account signs in to the managed realm itself, and ``<realm>-realm`` when
it signs in to ``master``. So the check reads ``resource_access.<admin
client>.roles`` of the connector's own admin token: no extra request and no
extra right. The token is only read here; it is never shown or stored.

When the roles cannot be read (the token is not a JWT, it has no
``resource_access`` claim, as with a lightweight access token, or the
account signs in to a third realm), the call still waits for approval. The
card says "MEHO could not check the rights of the target's account".
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._preview import (
    PermissionPreflight,
    PreviewBuilder,
    PreviewContext,
    register_permission_preflight,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "NO_RIGHTS_CHECK",
    "AccountRights",
    "KeycloakMissingRightsError",
    "names_missing_rights",
    "read_account_rights",
    "register_rights_check",
    "rights_text",
]

#: The card line when MEHO could not read the roles of the target's account.
NO_RIGHTS_CHECK: Final[str] = "MEHO could not check the rights of the target's account."

_MASTER_REALM: Final[str] = "master"
_REALM_MANAGEMENT: Final[str] = "realm-management"
_DOC_REF: Final[str] = "docs/codebase/connectors-keycloak.md#rights-of-the-login-flow-ops"

Handler = Callable[
    ["KeycloakConnector", "Operator", "KeycloakTargetLike", dict[str, Any]],
    Awaitable[dict[str, Any]],
]


class KeycloakMissingRightsError(ConnectorUpstreamError):
    """Keycloak answered 403 to a login flow op. ``remediation`` names the roles."""

    def __init__(self, message: str, *, upstream_message: str | None, remediation: str) -> None:
        super().__init__(message, http_status=403, upstream_message=upstream_message)
        self.remediation = remediation


@dataclass(frozen=True)
class AccountRights:
    """The admin roles of the target's account, as far as MEHO could read them.

    ``roles`` is ``None`` when they could not be read. ``client`` and
    ``admin_realm`` say where the roles live; ``client`` is ``None`` when
    the account signs in to a realm that cannot manage the managed realm.
    """

    account: str | None
    admin_realm: str
    client: str | None
    roles: frozenset[str] | None


def _admin_client(admin_realm: str, managed_realm: str) -> str | None:
    """The client whose roles give admin rights on *managed_realm*."""
    if admin_realm == _MASTER_REALM:
        return f"{managed_realm}-realm"
    if admin_realm == managed_realm:
        return _REALM_MANAGEMENT
    return None


def _claims(token: str) -> dict[str, Any] | None:
    """The payload of a JWT, read without a check of the signature; ``None`` when not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _client_roles(claims: dict[str, Any], client: str) -> frozenset[str] | None:
    """The roles of *client* in ``resource_access``; ``None`` when the claim is missing."""
    access = claims.get("resource_access")
    if not isinstance(access, dict):
        return None
    entry = access.get(client)
    if entry is None:
        return frozenset()
    roles = entry.get("roles") if isinstance(entry, dict) else None
    if not isinstance(roles, list):
        return None
    return frozenset(role for role in roles if isinstance(role, str))


async def read_account_rights(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike
) -> AccountRights:
    """Read the admin roles of the target's account from the connector's admin token.

    Never raises: any failure gives ``roles=None`` (could not check).
    """
    realms = resolve_realm_config(target)
    client = _admin_client(realms.admin_realm, realms.managed_realm)
    unknown = AccountRights(None, realms.admin_realm, client, None)
    try:
        token = await connector._admin_token(target, operator)
    except Exception:
        return unknown
    claims = _claims(token)
    if claims is None:
        return unknown
    name = claims.get("preferred_username") or claims.get("azp")
    account = name if isinstance(name, str) and name else None
    roles = _client_roles(claims, client) if client is not None else None
    return AccountRights(account, realms.admin_realm, client, roles)


def _missing(roles: tuple[str, ...], rights: AccountRights) -> list[str]:
    if rights.roles is None:
        return []
    return [role for role in roles if role not in rights.roles]


def _account(rights: AccountRights) -> str:
    return repr(rights.account) if rights.account else "of the target"


def _lacks_text(roles: tuple[str, ...], rights: AccountRights) -> str:
    """Short: the account and the roles it lacks (fits the 256-character reason)."""
    return (
        f"the account {_account(rights)} lacks the role(s) {', '.join(_missing(roles, rights))} "
        f"of the client {rights.client!r} in the realm {rights.admin_realm!r}"
    )


def rights_text(op_id: str, roles: tuple[str, ...], rights: AccountRights) -> str:
    """Name the roles *op_id* needs, where they live, and the ones the account lacks."""
    account = _account(rights)
    where = (
        f"of the client {rights.client!r} in the realm {rights.admin_realm!r}"
        if rights.client
        else f"for this realm (the account signs in to the realm {rights.admin_realm!r})"
    )
    text = f"{op_id} needs the role(s) {', '.join(roles)} {where} on the account {account}."
    missing = _missing(roles, rights)
    if missing:
        text += f" The account lacks: {', '.join(missing)}."
    return text + " Give the account the role(s) in Keycloak, then run the operation again."


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    """The live Keycloak connector of the park; ``None`` on the egress-free preview path."""
    if ctx.connector_instance is None or ctx.target is None:
        return None
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    return connector if isinstance(connector, KeycloakConnector) else None


def _preflight(op_id: str, roles: tuple[str, ...]) -> PermissionPreflight:
    async def check(ctx: PreviewContext) -> dict[str, Any] | None:
        connector = _live_connector(ctx)
        if connector is None:
            return None
        rights = await read_account_rights(connector, ctx.operator, ctx.target)
        if rights.roles is None:
            return None
        denied = bool(_missing(roles, rights))
        result: dict[str, Any] = {
            "check": "keycloak.admin_token_roles",
            "path": f"{rights.admin_realm}/{rights.client}",
            "account": rights.account,
            "required": list(roles),
            "granted": sorted(role for role in roles if role in rights.roles),
            "will_be_denied": denied,
        }
        if denied:
            result["reason"] = _lacks_text(roles, rights)
            result["remediation"] = (
                f"Give the account the role(s) in Keycloak, then call {op_id} again."
            )
            result["doc_ref"] = _DOC_REF
        return result

    return check


def _card(roles: tuple[str, ...], builder: PreviewBuilder) -> PreviewBuilder:
    async def build(ctx: PreviewContext) -> dict[str, Any] | None:
        preview = dict(await builder(ctx) or {})
        connector = _live_connector(ctx)
        rights = (
            await read_account_rights(connector, ctx.operator, ctx.target)
            if connector is not None
            else None
        )
        preview["roles_needed"] = list(roles)
        if rights is None or rights.roles is None:
            preview["rights_check"] = NO_RIGHTS_CHECK
        elif _missing(roles, rights):
            preview["rights_check"] = rights_text(ctx.descriptor.op_id, roles, rights)
        else:
            preview["rights_check"] = (
                f"MEHO checked the account {_account(rights)}: it has the role(s) "
                f"{', '.join(roles)} of the client {rights.client!r}."
            )
        return preview

    return build


def register_rights_check(op_id: str, roles: tuple[str, ...], builder: PreviewBuilder) -> None:
    """Register the check before the wait and the approval card of a login flow op.

    *builder* makes the rest of the card; the roles and the check line are
    added to it.
    """
    register_permission_preflight(op_id, _preflight(op_id, roles))
    register_preview_builder(op_id, _card(roles, builder))


def names_missing_rights(op_id: str, roles: tuple[str, ...]) -> Callable[[Handler], Handler]:
    """Turn a 403 of the handler into :class:`KeycloakMissingRightsError` naming the roles."""

    def wrap(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def run(
            connector: KeycloakConnector,
            operator: Operator,
            target: KeycloakTargetLike,
            params: dict[str, Any],
        ) -> dict[str, Any]:
            try:
                return await handler(connector, operator, target, params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403:
                    raise
                rights = await read_account_rights(connector, operator, target)
                raise KeycloakMissingRightsError(
                    f"{op_id} stopped: Keycloak refused a request with HTTP 403. "
                    "The target's account lacks a right; see extras.remediation.",
                    upstream_message=exc.response.text or None,
                    remediation=rights_text(op_id, roles, rights),
                ) from exc

        return run

    return wrap
