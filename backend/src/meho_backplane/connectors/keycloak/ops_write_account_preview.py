# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval cards for the Keycloak user account writes.

============================================  ===================================================
op_id                                         what the approver sees
============================================  ===================================================
``keycloak.role_mapping.client.assign``       user, client, role names, and the grant facts
``keycloak.role_mapping.client.remove``       user, client, role names, and for a
                                              ``realm-management`` role one ``warning`` line
                                              with how many users still hold it
``keycloak.user.update``                      user, the current user name and email
                                              (``current``), the new field values, and the
                                              grant facts
``keycloak.user.credential.delete``           the credentials that go and stay, one
                                              ``warning`` line when the user is left with no
                                              second factor or no credential, and grant facts
============================================  ===================================================

The holder count reads the users who hold the role directly (not through
a group), up to :data:`HOLDER_CAP` per role. A count MEHO cannot read is
"unknown" on the card. The ``current`` block of the user update card is
``None`` when MEHO cannot read the user.

The credential card reads the user's credentials when the request is
parked and shows each one as ``id``, ``type``, ``userLabel`` and
``createdDate`` only (the allow-list of
:mod:`~meho_backplane.connectors.keycloak.ops_write_user_credential`). It
shows every credential, with no cut. A request that matches nothing is
refused here, so it never reaches an approver. A ``credential_type`` call
removes only the credentials on the card: when the user's credentials of
that type changed while the request waited, the approved run is refused
(:data:`~meho_backplane.connectors.keycloak.ops_write_grant.CREDENTIALS_CHANGED`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_ROLE_USERS_PATH,
    _USER_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_account_common import (
    is_realm_management,
    opt_str,
    resolve_user,
)
from meho_backplane.connectors.keycloak.ops_write_client_role import role_names
from meho_backplane.connectors.keycloak.ops_write_grant import with_grant_facts
from meho_backplane.connectors.keycloak.ops_write_preview import (
    _live_keycloak_connector,
    _managed_realm,
)
from meho_backplane.connectors.keycloak.ops_write_user_credential import (
    match_credentials,
    read_credentials,
)
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import quote_segment
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

#: The most holders of one role MEHO counts for the remove card.
HOLDER_CAP: Final[int] = 1000

_HOLDER_PAGE: Final[int] = 100

#: Credential types that are a second factor (a one-time code, a passkey, recovery codes).
_SECOND_FACTOR_TYPES: Final[frozenset[str]] = frozenset(
    {"otp", "webauthn", "webauthn-passwordless", "recovery-authn-codes"}
)


async def _holders(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, client_uuid: str, role: str
) -> list[str] | None:
    """The ids of the users who hold *role* directly; ``None`` when unreadable or over the cap."""
    path = fill_path(
        _CLIENT_ROLE_USERS_PATH,
        {
            "realm": realm,
            "client-uuid": quote_segment(client_uuid),
            "role-name": quote_segment(role),
        },
    )
    ids: list[str] = []
    while len(ids) <= HOLDER_CAP:
        rows = await connector._get_admin_list(
            ctx.target,
            path,
            operator=ctx.operator,
            params={
                "first": str(len(ids)),
                "max": str(_HOLDER_PAGE),
                "briefRepresentation": "true",
            },
            strict=True,
        )
        page = [row["id"] for row in rows if isinstance(row, dict) and opt_str(row.get("id"))]
        if len(page) != len(rows):
            return None
        ids.extend(page)
        if len(rows) < _HOLDER_PAGE:
            break
    return None if len(ids) > HOLDER_CAP else ids


async def _holders_after_remove(ctx: PreviewContext, names: list[str]) -> dict[str, int | None]:
    """Per role: how many users still hold it directly after the remove; ``None`` when unknown."""
    counts: dict[str, int | None] = dict.fromkeys(names)
    connector = _live_keycloak_connector(ctx)
    if connector is None:
        return counts
    try:
        uuid, _username, realm = await resolve_user(
            connector,
            ctx.operator,
            ctx.target,
            ctx.params,
            op_id="keycloak.role_mapping.client.remove",
        )
        client_uuid = await connector._find_client_uuid(
            ctx.target, realm, str(ctx.params.get("client_id")).strip(), operator=ctx.operator
        )
    except Exception:
        return counts
    if client_uuid is None:
        return counts
    for name in names:
        try:
            holders = await _holders(connector, ctx, realm, client_uuid, name)
        except Exception:
            holders = None
        if holders is not None:
            counts[name] = len([h for h in holders if h != uuid])
    return counts


def _client_role_preview(action: str) -> Any:
    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        realm = _managed_realm(ctx)
        names = role_names(ctx.params)
        preview: dict[str, Any] = {
            "resource": "client_role_mapping",
            "action": action,
            "username": opt_str(ctx.params.get("username")),
            "id": opt_str(ctx.params.get("id")),
            "client_id": opt_str(ctx.params.get("client_id")),
            "realm": realm,
            "roles": names,
        }
        if action == "remove" and is_realm_management(ctx.params):
            counts = await _holders_after_remove(ctx, names)
            held = ", ".join(
                f"{name}: {'unknown' if count is None else count}" for name, count in counts.items()
            )
            preview["holders_after"] = counts
            preview["warning"] = (
                f"These are realm-management roles: they decide who may manage realm {realm}. "
                f"Users who still hold each role directly after this change: {held}."
            )
        return preview

    return _builder


async def _current_user(ctx: PreviewContext) -> dict[str, Any] | None:
    """The user's current user name and email; ``None`` when they cannot be read."""
    connector = _live_keycloak_connector(ctx)
    if connector is None:
        return None
    try:
        uuid, _username, realm = await resolve_user(
            connector, ctx.operator, ctx.target, ctx.params, op_id="keycloak.user.update"
        )
        row = await connector._get_admin_json(
            ctx.target,
            fill_path(_USER_PATH, {"realm": realm, "user-id": quote_segment(uuid)}),
            operator=ctx.operator,
        )
    except Exception:
        return None
    return {"username": opt_str(row.get("username")), "email": opt_str(row.get("email"))}


async def _user_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    changes = ctx.params.get("changes")
    return {
        "resource": "user",
        "action": "update",
        "username": opt_str(ctx.params.get("username")),
        "id": opt_str(ctx.params.get("id")),
        "realm": _managed_realm(ctx),
        "current": await _current_user(ctx),
        "changes": redact_secret_fields(changes) if isinstance(changes, dict) else {},
    }


def _credential_warning(current: list[dict[str, Any]], kept: list[dict[str, Any]]) -> str | None:
    """One line when the delete leaves the user with no credential, or no second factor."""
    if not kept:
        return "After this change the user has no stored credential at all."

    def _second_factors(rows: list[dict[str, Any]]) -> bool:
        return any(str(row.get("type", "")).casefold() in _SECOND_FACTOR_TYPES for row in rows)

    if _second_factors(current) and not _second_factors(kept):
        return "After this change the user has no second factor (no one-time code and no passkey)."
    return None


async def _credential_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    connector = _live_keycloak_connector(ctx)
    if connector is None:
        return None
    op_id = "keycloak.user.credential.delete"
    uuid, username, realm = await resolve_user(
        connector, ctx.operator, ctx.target, ctx.params, op_id=op_id
    )
    current = await read_credentials(connector, ctx.operator, ctx.target, realm=realm, user_id=uuid)
    matches = match_credentials(current, ctx.params, op_id=op_id)
    ids = {entry["id"] for entry in matches}
    kept = [entry for entry in current if entry.get("id") not in ids]
    preview: dict[str, Any] = {
        "resource": "user_credential",
        "action": "delete",
        "id": uuid,
        "username": username,
        "realm": realm,
        "will_remove": matches,
        "will_keep": kept,
    }
    if (warning := _credential_warning(current, kept)) is not None:
        preview["warning"] = warning
    return preview


def _register() -> None:
    register_preview_builder(
        "keycloak.role_mapping.client.assign",
        with_grant_facts("keycloak.role_mapping.client.assign", _client_role_preview("assign")),
    )
    register_preview_builder("keycloak.role_mapping.client.remove", _client_role_preview("remove"))
    register_preview_builder(
        "keycloak.user.update", with_grant_facts("keycloak.user.update", _user_update_preview)
    )
    register_preview_builder(
        "keycloak.user.credential.delete",
        with_grant_facts("keycloak.user.credential.delete", _credential_delete_preview),
    )


_register()
