# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval cards for the Keycloak user account writes.

============================================  ===================================================
op_id                                         what the approver sees
============================================  ===================================================
``keycloak.role_mapping.client.assign``       user, client, role names, and the grant facts
``keycloak.role_mapping.client.remove``       user, client, role names
``keycloak.user.update``                      user, and the new values of the fields to change
``keycloak.user.credential.delete``           the credentials that will go and the ones that stay
============================================  ===================================================

The credential card reads the user's credentials when the request is
parked and shows each one as ``id``, ``type``, ``userLabel`` and
``createdDate`` only (the allow-list of
:mod:`~meho_backplane.connectors.keycloak.ops_write_user_credential`). It
shows every credential, with no cut. A request that matches nothing is
refused here, so it never reaches an approver. A ``credential_type`` call
removes every credential of that type that the user has when it runs.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_write_account_common import opt_str, resolve_user
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
from meho_backplane.operations._preview import PreviewContext, register_preview_builder


def _client_role_preview(action: str) -> Any:
    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        return {
            "resource": "client_role_mapping",
            "action": action,
            "username": opt_str(ctx.params.get("username")),
            "id": opt_str(ctx.params.get("id")),
            "client_id": opt_str(ctx.params.get("client_id")),
            "realm": _managed_realm(ctx),
            "roles": role_names(ctx.params),
        }

    return _builder


async def _user_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    changes = ctx.params.get("changes")
    return {
        "resource": "user",
        "action": "update",
        "username": opt_str(ctx.params.get("username")),
        "id": opt_str(ctx.params.get("id")),
        "realm": _managed_realm(ctx),
        "changes": redact_secret_fields(changes) if isinstance(changes, dict) else {},
    }


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
    return {
        "resource": "user_credential",
        "action": "delete",
        "id": uuid,
        "username": username,
        "realm": realm,
        "will_remove": matches,
        "will_keep": [entry for entry in current if entry.get("id") not in ids],
    }


def _register() -> None:
    register_preview_builder(
        "keycloak.role_mapping.client.assign",
        with_grant_facts("keycloak.role_mapping.client.assign", _client_role_preview("assign")),
    )
    register_preview_builder("keycloak.role_mapping.client.remove", _client_role_preview("remove"))
    register_preview_builder("keycloak.user.update", _user_update_preview)
    register_preview_builder("keycloak.user.credential.delete", _credential_delete_preview)


_register()
