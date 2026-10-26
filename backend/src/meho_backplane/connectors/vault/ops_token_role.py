# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault *token role* op handlers, spec table and write preflight.

A token role is a saved set of rules in Vault for making tokens: which
policies a token may get, whether it has a parent token, and how long it
lives between renewals. Vault keeps them on the core ``token`` auth
backend under ``auth/token/roles/<role_name>``.

Three ops in the ``token`` group:

* ``vault.token.role.write`` -- create a role, or change one
  (``POST auth/token/roles/<role_name>``; dangerous, needs approval).
* ``vault.token.role.read`` -- one role's settings (safe).
* ``vault.token.role.list`` -- the role names (safe). An empty store
  (Vault ``404``) is ``{"keys": []}``, like ``vault.token.list_accessors``.

The write sends **only the fields the caller gives**. Vault's token-role
write changes just the fields present in the request body and keeps the
others, but hvac's ``auth.token.create_or_update_role`` always sends
``orphan`` and ``renewable`` (they have defaults), so a change to one
field would silently reset those two. The handler therefore posts the
body itself through ``client.adapter.post`` on the same API path hvac
uses.

Login goes through
:func:`~meho_backplane.connectors.vault.target_auth.vault_client_for_target`,
so the target's ``extras["vault_role"]`` decides which Vault role makes
the change. A role holds no secret, so nothing is redacted; the write
classifies ``write`` through its ``.write`` suffix.

The write also registers a park-time permission preflight (imported here
at module load, like the KV write previews): before MEHO asks for
approval, ``sys/capabilities-self`` must grant ``create`` and ``update``
on ``auth/token/roles/<role_name>`` to the target's role. When it does
not, the call is refused with ``write_capability_denied`` and no approval
request is made.
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac.exceptions

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops import vault_capability_preflight
from meho_backplane.connectors.vault.ops_auth import _extract_data, _extract_keys
from meho_backplane.connectors.vault.ops_token_role_schemas import (
    VAULT_TOKEN_ROLE_LIST_LLM_INSTRUCTIONS,
    VAULT_TOKEN_ROLE_LIST_PARAMETER_SCHEMA,
    VAULT_TOKEN_ROLE_LIST_RESPONSE_SCHEMA,
    VAULT_TOKEN_ROLE_READ_LLM_INSTRUCTIONS,
    VAULT_TOKEN_ROLE_READ_PARAMETER_SCHEMA,
    VAULT_TOKEN_ROLE_READ_RESPONSE_SCHEMA,
    VAULT_TOKEN_ROLE_WRITE_LLM_INSTRUCTIONS,
    VAULT_TOKEN_ROLE_WRITE_PARAMETER_SCHEMA,
    VAULT_TOKEN_ROLE_WRITE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.operations._preview import PreviewContext, register_permission_preflight

__all__ = [
    "TOKEN_ROLE_OP_LIST",
    "TOKEN_ROLE_OP_READ",
    "TOKEN_ROLE_OP_SPECS",
    "TOKEN_ROLE_OP_WRITE",
    "TOKEN_ROLE_WRITE_CAPABILITIES",
    "token_role_api_path",
    "token_role_auth_path",
    "vault_token_role_list",
    "vault_token_role_read",
    "vault_token_role_write",
    "vault_token_role_write_capability_preflight",
]

TOKEN_ROLE_OP_WRITE = "vault.token.role.write"
TOKEN_ROLE_OP_READ = "vault.token.role.read"
TOKEN_ROLE_OP_LIST = "vault.token.role.list"

#: Vault ACL capabilities the write needs on ``auth/token/roles/<role_name>``.
#: Vault asks for ``create`` when the role does not exist yet and ``update``
#: when it does, so an unconditional write needs both.
TOKEN_ROLE_WRITE_CAPABILITIES: frozenset[str] = frozenset({"create", "update"})

#: Role settings forwarded to Vault verbatim when the caller gives them.
#: Each is optional on the Vault side; a field that is not sent keeps its
#: current value on an update and its default on a create.
_TOKEN_ROLE_WRITE_FIELDS: tuple[str, ...] = (
    "allowed_policies",
    "disallowed_policies",
    "orphan",
    "renewable",
    "token_period",
    "token_explicit_max_ttl",
)


def token_role_auth_path(role_name: str) -> str:
    """Return the path Vault authorizes a token role on: ``auth/token/roles/<role_name>``."""
    return f"auth/token/roles/{role_name}"


def token_role_api_path(role_name: str) -> str:
    """Return the API path of a token role: ``/v1/auth/token/roles/<role_name>``."""
    return f"/v1/{token_role_auth_path(role_name)}"


async def vault_token_role_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create a token role, or change one (``POST auth/token/roles/<role_name>``).

    Op-id: ``vault.token.role.write``. Sends only the settings the caller
    gave, so an update of one field leaves the others as they are (see the
    module docstring for why this bypasses hvac's helper). Returns the
    settings sent; no token is made.
    """
    role_name: str = str(params["role_name"]).strip()
    settings: dict[str, Any] = {
        field: params[field] for field in _TOKEN_ROLE_WRITE_FIELDS if params.get(field) is not None
    }

    async with vault_client_for_target(operator, target) as client:
        await asyncio.to_thread(client.adapter.post, token_role_api_path(role_name), json=settings)

    return {"role_name": role_name, "written": True, "settings": settings}


async def vault_token_role_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one token role's settings (``GET auth/token/roles/<role_name>``).

    Op-id: ``vault.token.role.read``. Delegates to hvac's
    ``client.auth.token.read_role(role_name=...)`` and returns the
    unwrapped ``data`` dict. An unknown role has no "legitimately empty"
    case, so the underlying :class:`hvac.exceptions.InvalidPath` surfaces
    as a read-phase ``connector_error`` (like ``vault.token.lookup_accessor``).
    """
    role_name: str = str(params["role_name"]).strip()

    async with vault_client_for_target(operator, target) as client:
        payload = await asyncio.to_thread(client.auth.token.read_role, role_name=role_name)

    return _extract_data(payload)


async def vault_token_role_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List the token role names (``LIST auth/token/roles``).

    Op-id: ``vault.token.role.list``. Delegates to hvac's
    ``client.auth.token.list_roles()``. Vault answers ``404`` when there
    are no roles; that is normalised to ``{"keys": []}``.
    """
    async with vault_client_for_target(operator, target) as client:
        try:
            payload = await asyncio.to_thread(client.auth.token.list_roles)
        except hvac.exceptions.InvalidPath:
            return {"keys": []}

    return {"keys": _extract_keys(payload)}


async def vault_token_role_write_capability_preflight(
    operator: Operator, params: dict[str, Any], target: Any = None
) -> dict[str, Any] | None:
    """Check whether the target's role may write ``auth/token/roles/<role_name>``.

    Runs at approval-park time through the shared
    :func:`~meho_backplane.connectors.vault.ops.vault_capability_preflight`
    probe: ``sys/capabilities-self`` under the target's Vault role must
    grant :data:`TOKEN_ROLE_WRITE_CAPABILITIES`. Returns the probe's
    secret-free summary, or ``None`` when the probe itself fails.
    """
    role_name = str(params.get("role_name", "")).strip()
    return await vault_capability_preflight(
        operator,
        TOKEN_ROLE_OP_WRITE,
        token_role_auth_path(role_name),
        TOKEN_ROLE_WRITE_CAPABILITIES,
        target,
    )


async def _token_role_write_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Permission preflight hook for ``vault.token.role.write``."""
    return await vault_token_role_write_capability_preflight(
        ctx.operator, ctx.params, target=ctx.target
    )


register_permission_preflight(TOKEN_ROLE_OP_WRITE, _token_role_write_preflight)


# --- spec table ------------------------------------------------------------

#: Per-op registration spec for the token-role ops. ``group_key`` is
#: ``token`` for every row. Consumed by the package composer
#: ``ops_identity_token.register_vault_identity_token_operations`` next to
#: ``TOKEN_OP_SPECS``.
TOKEN_ROLE_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": TOKEN_ROLE_OP_WRITE,
        "handler": vault_token_role_write,
        "group_key": "token",
        "summary": "Create a Vault token role, or change one.",
        "description": (
            "Creates a token role or changes one that exists (POST "
            "auth/token/roles/<role_name>). A token role is a saved set of "
            "rules for making tokens: allowed policies, orphan, renewable, "
            "period and maximum lifetime. Only the fields given are sent. "
            "Dangerous; needs approval."
        ),
        "parameter_schema": VAULT_TOKEN_ROLE_WRITE_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_ROLE_WRITE_RESPONSE_SCHEMA,
        "tags": ["write", "token", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_TOKEN_ROLE_WRITE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": TOKEN_ROLE_OP_READ,
        "handler": vault_token_role_read,
        "group_key": "token",
        "summary": "Read the settings of one Vault token role.",
        "description": (
            "Reads one token role's settings (GET auth/token/roles/<role_name>): "
            "allowed and disallowed policies, orphan, renewable, period and "
            "maximum lifetime. A role holds no secret. Read-only; safe, no "
            "approval. An unknown role is a not-found error."
        ),
        "parameter_schema": VAULT_TOKEN_ROLE_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_ROLE_READ_RESPONSE_SCHEMA,
        "tags": ["read-only", "token", "identity"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_TOKEN_ROLE_READ_LLM_INSTRUCTIONS,
    },
    {
        "op_id": TOKEN_ROLE_OP_LIST,
        "handler": vault_token_role_list,
        "group_key": "token",
        "summary": "List the names of the Vault token roles.",
        "description": (
            "Lists the token role names (LIST auth/token/roles). Names only, "
            "no secret content. Read-only; safe, no approval. When there are "
            "no roles the answer is {'keys': []}."
        ),
        "parameter_schema": VAULT_TOKEN_ROLE_LIST_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_ROLE_LIST_RESPONSE_SCHEMA,
        "tags": ["read-only", "token", "identity"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_TOKEN_ROLE_LIST_LLM_INSTRUCTIONS,
    },
)
