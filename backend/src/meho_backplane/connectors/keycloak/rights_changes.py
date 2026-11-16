# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which Keycloak writes are changes of rights.

MEHO reads a person's rights from the login token, and Keycloak builds
that token. So a Keycloak write that can change what a token says, who
has which role, who may sign in, or how strong a sign-in is, is a change
of rights. For each such write:

* the requester never approves their own request;
* another admin (a tenant admin or a platform admin) must approve it;
* a standing grant, a stored grant row and a run approval never cover it,
  so each call waits for its own approval. An agent's call waits too.

Every Keycloak write is on exactly one of the two tables below, with the
reason. ``tests/test_keycloak_rights_change_writes.py`` fails when a new
Keycloak write is on neither table, so each new write gets this decision.

:func:`register_rights_change_ops` registers every write of
:data:`RIGHTS_CHANGE_OPS` with
:func:`~meho_backplane.operations.grant_approval.register_rights_change_op`,
in every realm. That adds the per-call mark. The writes of one login or
one group (``GRANT_OPS`` in :mod:`.ops_write_grant`) are also registered
there with
:func:`~meho_backplane.operations.grant_approval.register_grant_op`, which
also checks who the change is for.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.operations.grant_approval import register_rights_change_op

__all__ = ["NOT_RIGHTS_CHANGE_OPS", "RIGHTS_CHANGE_OPS", "register_rights_change_ops"]

#: op_id -> how the write can change what a token says, who has a role,
#: who may sign in, or how strong a sign-in is.
RIGHTS_CHANGE_OPS: Final[dict[str, str]] = {
    "keycloak.realm.create": (
        "the realm body can hold users, groups, roles, clients, mappers and default roles"
    ),
    "keycloak.realm.update": (
        "realm settings decide default roles and groups, default client scopes, "
        "self sign-up and which user details a user may change"
    ),
    "keycloak.client.create": (
        "a client carries protocol mappers, default and optional scopes, "
        "full scope and a service account login"
    ),
    "keycloak.client.update": (
        "changes the mappers, scopes, full scope, service account, redirect URIs, "
        "secret and login settings of a client"
    ),
    "keycloak.client_scope.create": (
        "a client scope carries protocol mappers and role scope mappings for tokens"
    ),
    "keycloak.protocol_mapper.create": "a protocol mapper decides which claims a token holds",
    "keycloak.user.reset_password": (
        "a new password for a login is a change of rights; another admin approves it"
    ),
    "keycloak.user.create": (
        "the new user can carry groups, roles and attributes that mappers put into tokens"
    ),
    "keycloak.role_mapping.assign": "gives a user realm roles",
    "keycloak.role_mapping.client.assign": "gives a user roles of a client",
    "keycloak.user.update": (
        "changes a user's user name and email, which tokens carry and which a login "
        "or a password reset uses"
    ),
    "keycloak.user.credential.delete": (
        "removes a user's passkey or one-time code, so it changes how that login signs in"
    ),
    "keycloak.user.enable": "turns a login on, so it decides who may sign in",
    "keycloak.user.required_actions.update": (
        "sets the steps a user must do at the next sign-in, such as setting up a "
        "passkey or a one-time code, so it changes how strong that sign-in is"
    ),
    "keycloak.group.member.add": "puts a user into a group and gives them its roles",
    "keycloak.group.update_attributes": ("group attributes fill the tenant claims of every member"),
    "keycloak.authflow.ensure_approval_flow": (
        "maps the acr name 'meho-approve' to a sign-in level in the realm; MEHO reads "
        "that acr claim of a token to accept an approval"
    ),
    "keycloak.authflow.ensure_passkey_code_skip": (
        "binds MEHO's own copy of the browser sign-in flow, so it changes the steps "
        "and the strength of every sign-in in the realm"
    ),
    "keycloak.authflow.ensure_approval_touch": (
        "adds or changes the client's 'amr' protocol mapper, which puts a claim into "
        "every token, and maps the acr name 'meho-touch' to a sign-in level"
    ),
}

#: op_id -> why the write cannot change what a token says, who has a role,
#: who may sign in, or how strong a sign-in is.
NOT_RIGHTS_CHANGE_OPS: Final[dict[str, str]] = {
    "keycloak.user.check_login": "tests one sign-in and changes nothing",
    "keycloak.user.logout": "ends sessions; it only takes away",
    "keycloak.user.disable": "turns a login off; it only takes away",
    "keycloak.user.delete": "removes a user; it only takes away",
    "keycloak.group.create": (
        "a new group has no members; adding one is keycloak.group.member.add"
    ),
    "keycloak.group.member.remove": "takes a user out of a group; it only takes away",
    "keycloak.role_mapping.client.remove": "takes client roles from a user; it only takes away",
    "keycloak.group.delete": "removes a group; it only takes away",
    "keycloak.client.delete": "removes a client; it only takes away",
    "keycloak.realm.delete": "removes a realm; it only takes away",
}


def register_rights_change_ops() -> None:
    """Register every write of :data:`RIGHTS_CHANGE_OPS` as a change of rights."""
    for op_id in RIGHTS_CHANGE_OPS:
        register_rights_change_op(op_id)


register_rights_change_ops()
