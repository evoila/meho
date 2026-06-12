# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault auth credential-lifecycle write handlers + registration (G3.15-T3).

Op-id namespace: ``vault.auth.<backend>.<verb>``. This module ships the
**write half** of the userpass + approle auth surface -- the credential
lifecycle the consumer's ``/vault-admin`` wrapper exercises:

* ``vault.auth.userpass.write`` -- ``create_or_update_user`` (dangerous;
  password in params -> ``credential_write``; binds ``token_policies``).
* ``vault.auth.userpass.update_password`` -- ``update_password_on_user``
  (caution; password in params -> ``credential_write``).
* ``vault.auth.userpass.delete`` -- ``delete_user`` (dangerous).
* ``vault.auth.approle.write`` -- ``create_or_update_approle`` (dangerous).
* ``vault.auth.approle.delete`` -- ``delete_role`` (dangerous).
* ``vault.auth.approle.generate_secret_id`` -- ``generate_secret_id``
  (dangerous; SecretID in the **response** -> ``credential_mint``;
  non-idempotent -- mints a fresh SecretID each call).

Every op registers ``requires_approval=True``: each is a privilege
assignment, an irreversible identity removal, or a secret mint. The
production-path approval routing (a human/service principal floors to
the approval queue) is owned by the G11.7-T1 (#1401) policy gate; this
module only marks the descriptors.

**Secret redaction is at the classification layer, not the handler**
(#1411 / #1401). The secret never reaches the audit row -- the audit
payload stores a ``params_hash``, never the raw params -- and never
reaches the broadcast feed, because
:func:`meho_backplane.broadcast.events.classify_op` maps the two
password ops to ``credential_write`` (request-secret, aggregate-only
broadcast) and ``generate_secret_id`` to ``credential_mint``
(response-secret, aggregate-only broadcast). The handlers add a second
layer of defence by returning **value-free** confirmations for the
write ops (username/role + assigned policies, never the password); the
``generate_secret_id`` handler is the one exception -- it returns the
minted SecretID to the caller's ``OperationResult`` (the whole point of
minting), which classification keeps out of audit + broadcast.

Handler shape mirrors
:mod:`meho_backplane.connectors.vault.ops_auth` verbatim: each is a
module-level ``async def`` with the ``(operator, target, params) ->
dict`` typed-op contract, raises on failure (the dispatcher's
``connector_error`` branch records the class name in
``extras["exception_class"]``), forwards the operator JWT via
``vault_client_for_operator``, and offloads the blocking hvac call with
``asyncio.to_thread``. The backend-not-mounted reclassification reuses
the read module's :class:`VaultAuthBackendNotMountedError` and probe
helper so a 404 from an unmounted backend surfaces the same
operator-actionable error class as the read ops.
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac.exceptions

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops_auth import (
    VaultAuthBackendNotMountedError,
    _reclassify_not_found,
)
from meho_backplane.connectors.vault.ops_auth_write_schemas import (
    VAULT_AUTH_APPROLE_DELETE_LLM_INSTRUCTIONS,
    VAULT_AUTH_APPROLE_DELETE_PARAMETER_SCHEMA,
    VAULT_AUTH_APPROLE_DELETE_RESPONSE_SCHEMA,
    VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_LLM_INSTRUCTIONS,
    VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_PARAMETER_SCHEMA,
    VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_RESPONSE_SCHEMA,
    VAULT_AUTH_APPROLE_WRITE_LLM_INSTRUCTIONS,
    VAULT_AUTH_APPROLE_WRITE_PARAMETER_SCHEMA,
    VAULT_AUTH_APPROLE_WRITE_RESPONSE_SCHEMA,
    VAULT_AUTH_USERPASS_DELETE_LLM_INSTRUCTIONS,
    VAULT_AUTH_USERPASS_DELETE_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_DELETE_RESPONSE_SCHEMA,
    VAULT_AUTH_USERPASS_UPDATE_PASSWORD_LLM_INSTRUCTIONS,
    VAULT_AUTH_USERPASS_UPDATE_PASSWORD_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_UPDATE_PASSWORD_RESPONSE_SCHEMA,
    VAULT_AUTH_USERPASS_WRITE_LLM_INSTRUCTIONS,
    VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_WRITE_RESPONSE_SCHEMA,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "register_vault_auth_write_operations",
    "vault_auth_approle_delete",
    "vault_auth_approle_generate_secret_id",
    "vault_auth_approle_write",
    "vault_auth_userpass_delete",
    "vault_auth_userpass_update_password",
    "vault_auth_userpass_write",
]


# --- userpass write handlers -----------------------------------------------


async def vault_auth_userpass_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create or update a userpass user (password + token policies).

    Op-id: ``vault.auth.userpass.write``. Delegates to hvac's
    ``client.auth.userpass.create_or_update_user(username=...,
    password=..., token_policies=..., mount_point=...)``. ``token_policies``
    is forwarded as a kwarg so Vault's modern ``token_policies`` field is
    set (the legacy ``policies`` alias is left untouched).

    The password is in the request params and is classified
    ``credential_write`` -- redacted from the audit row (params_hash
    only) and the broadcast feed (aggregate-only). The response is
    value-free: it echoes the username, mount, and assigned policies,
    never the password.
    """
    username: str = str(params["username"]).strip()
    password: str = str(params["password"])
    mount: str = str(params.get("mount", "userpass")).strip()
    token_policies = params.get("token_policies")

    kwargs: dict[str, Any] = {
        "username": username,
        "password": password,
        "mount_point": mount,
    }
    if token_policies is not None:
        kwargs["token_policies"] = list(token_policies)

    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            await asyncio.to_thread(client.auth.userpass.create_or_update_user, **kwargs)
        except hvac.exceptions.InvalidPath as exc:
            raise VaultAuthBackendNotMountedError(
                f"userpass auth backend not mounted at {mount!r}"
            ) from exc

    result: dict[str, Any] = {"username": username, "mount": mount, "written": True}
    if token_policies is not None:
        result["token_policies"] = list(token_policies)
    return result


async def vault_auth_userpass_update_password(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Rotate an existing userpass user's password.

    Op-id: ``vault.auth.userpass.update_password``. Delegates to hvac's
    ``client.auth.userpass.update_password_on_user(username=...,
    password=..., mount_point=...)``. Password-only change: token
    policies are untouched.

    The new password is in the request params and classifies
    ``credential_write`` -- redacted from audit + broadcast. The response
    is value-free (username + mount + confirmation flag).

    A missing user under a *mounted* backend surfaces as the underlying
    :class:`hvac.exceptions.InvalidPath`; only the backend-absent case is
    reclassified to :class:`VaultAuthBackendNotMountedError` (the read
    handlers' probe contract).
    """
    username: str = str(params["username"]).strip()
    password: str = str(params["password"])
    mount: str = str(params.get("mount", "userpass")).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            await asyncio.to_thread(
                client.auth.userpass.update_password_on_user,
                username=username,
                password=password,
                mount_point=mount,
            )
        except hvac.exceptions.InvalidPath as exc:
            await _reclassify_not_found(client, exc, mount=mount, backend="userpass")

    return {"username": username, "mount": mount, "password_updated": True}


async def vault_auth_userpass_delete(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Delete a userpass user.

    Op-id: ``vault.auth.userpass.delete``. Delegates to hvac's
    ``client.auth.userpass.delete_user(username=..., mount_point=...)``.
    Vault's delete is idempotent (deleting a non-existent user is a
    no-op success), so a ``404`` here only ever means the backend itself
    is not mounted -- reclassified to
    :class:`VaultAuthBackendNotMountedError`.
    """
    username: str = str(params["username"]).strip()
    mount: str = str(params.get("mount", "userpass")).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            await asyncio.to_thread(
                client.auth.userpass.delete_user,
                username=username,
                mount_point=mount,
            )
        except hvac.exceptions.InvalidPath as exc:
            raise VaultAuthBackendNotMountedError(
                f"userpass auth backend not mounted at {mount!r}"
            ) from exc

    return {"username": username, "mount": mount, "deleted": True}


# --- approle write handlers ------------------------------------------------

#: AppRole config keys forwarded to hvac's ``create_or_update_approle``
#: verbatim when present in params. Each is optional on the Vault side
#: (omitting leaves the field unchanged on an update / at its default on
#: a create), so the handler only passes the keys the caller supplied.
_APPROLE_WRITE_FIELDS: tuple[str, ...] = (
    "token_policies",
    "token_ttl",
    "token_max_ttl",
    "secret_id_ttl",
    "secret_id_num_uses",
    "bind_secret_id",
)


async def vault_auth_approle_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create or update an AppRole's token/SecretID config.

    Op-id: ``vault.auth.approle.write``. Delegates to hvac's
    ``client.auth.approle.create_or_update_approle(role_name=...,
    mount_point=..., **config)``. Only the config keys the caller
    supplied are forwarded (each is optional on the Vault side). Mints no
    SecretID -- that is ``generate_secret_id``.
    """
    role_name: str = str(params["role_name"]).strip()
    mount: str = str(params.get("mount", "approle")).strip()

    kwargs: dict[str, Any] = {"role_name": role_name, "mount_point": mount}
    for field in _APPROLE_WRITE_FIELDS:
        if field in params and params[field] is not None:
            kwargs[field] = params[field]

    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            await asyncio.to_thread(client.auth.approle.create_or_update_approle, **kwargs)
        except hvac.exceptions.InvalidPath as exc:
            raise VaultAuthBackendNotMountedError(
                f"approle auth backend not mounted at {mount!r}"
            ) from exc

    return {"role_name": role_name, "mount": mount, "written": True}


async def vault_auth_approle_delete(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Delete an AppRole.

    Op-id: ``vault.auth.approle.delete``. Delegates to hvac's
    ``client.auth.approle.delete_role(role_name=..., mount_point=...)``.
    Idempotent like userpass delete; a ``404`` only means the backend is
    not mounted.
    """
    role_name: str = str(params["role_name"]).strip()
    mount: str = str(params.get("mount", "approle")).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            await asyncio.to_thread(
                client.auth.approle.delete_role,
                role_name=role_name,
                mount_point=mount,
            )
        except hvac.exceptions.InvalidPath as exc:
            raise VaultAuthBackendNotMountedError(
                f"approle auth backend not mounted at {mount!r}"
            ) from exc

    return {"role_name": role_name, "mount": mount, "deleted": True}


async def vault_auth_approle_generate_secret_id(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Mint a fresh SecretID for an AppRole (NON-IDEMPOTENT).

    Op-id: ``vault.auth.approle.generate_secret_id``. Delegates to hvac's
    ``client.auth.approle.generate_secret_id(role_name=..., metadata=...,
    cidr_list=..., token_bound_cidrs=..., mount_point=...)``.

    Each call mints a brand-new SecretID -- the op is non-idempotent.
    The SecretID lands in the *response*, so the op classifies
    ``credential_mint``: the broadcast collapses to aggregate-only and
    the audit row holds only a params_hash, so the minted SecretID never
    reaches the feed or the audit store. The caller's
    ``OperationResult`` still carries it (the whole point of minting) --
    it must be stored immediately.

    A missing role under a *mounted* backend surfaces as the underlying
    :class:`hvac.exceptions.InvalidPath`; only the backend-absent case is
    reclassified to :class:`VaultAuthBackendNotMountedError`.
    """
    role_name: str = str(params["role_name"]).strip()
    mount: str = str(params.get("mount", "approle")).strip()

    kwargs: dict[str, Any] = {"role_name": role_name, "mount_point": mount}
    for field in ("metadata", "cidr_list", "token_bound_cidrs"):
        if field in params and params[field] is not None:
            kwargs[field] = params[field]

    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            payload = await asyncio.to_thread(client.auth.approle.generate_secret_id, **kwargs)
        except hvac.exceptions.InvalidPath as exc:
            await _reclassify_not_found(client, exc, mount=mount, backend="approle")

    data = dict(payload["data"])
    result: dict[str, Any] = {
        "secret_id": data["secret_id"],
        "role_name": role_name,
        "mount": mount,
    }
    if "secret_id_accessor" in data:
        result["secret_id_accessor"] = data["secret_id_accessor"]
    if "secret_id_ttl" in data:
        result["secret_id_ttl"] = data["secret_id_ttl"]
    return result


# --- registration ----------------------------------------------------------

#: Per-op registration spec for the auth-write surface. Each row carries
#: its ``safety_level`` (the load-bearing signal the future policy gate
#: keys on) and ``tags``; all register ``requires_approval=True`` and
#: ``group_key="auth"`` in :func:`register_vault_auth_write_operations`.
_AUTH_WRITE_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": "vault.auth.userpass.write",
        "handler": vault_auth_userpass_write,
        "summary": "Create or update a userpass user (password + token policies).",
        "description": (
            "Creates a new userpass user or updates an existing one's "
            "password and/or token policies (POST "
            "/v1/auth/<mount>/users/<user>). The password rides in the "
            "request params and is classified credential_write: redacted "
            "from the audit row (params_hash only) and the broadcast feed "
            "(aggregate-only). Binding token_policies is a privilege "
            "assignment -- safety_level=dangerous, requires_approval=True. "
            "The response is value-free (username, mount, assigned "
            "policies; never the password). userpass not enabled raises "
            "VaultAuthBackendNotMountedError, surfaced as a "
            "connector_error OperationResult."
        ),
        "parameter_schema": VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_USERPASS_WRITE_RESPONSE_SCHEMA,
        "tags": ["write", "credential-write", "auth", "identity"],
        "safety_level": "dangerous",
        "llm_instructions": VAULT_AUTH_USERPASS_WRITE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.userpass.update_password",
        "handler": vault_auth_userpass_update_password,
        "summary": "Rotate an existing userpass user's password.",
        "description": (
            "Rotates an existing userpass user's password without "
            "touching their token policies (POST "
            "/v1/auth/<mount>/users/<user>/password). The new password "
            "rides in the request params and is classified "
            "credential_write: redacted from the audit row and the "
            "broadcast feed. safety_level=caution, requires_approval=True. "
            "The response is value-free. A missing user under a mounted "
            "backend surfaces as InvalidPath; userpass not enabled raises "
            "VaultAuthBackendNotMountedError."
        ),
        "parameter_schema": VAULT_AUTH_USERPASS_UPDATE_PASSWORD_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_USERPASS_UPDATE_PASSWORD_RESPONSE_SCHEMA,
        "tags": ["write", "credential-write", "auth", "identity"],
        "safety_level": "caution",
        "llm_instructions": VAULT_AUTH_USERPASS_UPDATE_PASSWORD_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.userpass.delete",
        "handler": vault_auth_userpass_delete,
        "summary": "Delete a userpass user.",
        "description": (
            "Deletes a userpass user, revoking their login (DELETE "
            "/v1/auth/<mount>/users/<user>). Irreversible removal of a "
            "login identity -- safety_level=dangerous, "
            "requires_approval=True. Idempotent (deleting a non-existent "
            "user is a no-op success), so a 404 only means userpass is "
            "not enabled at the mount -> VaultAuthBackendNotMountedError."
        ),
        "parameter_schema": VAULT_AUTH_USERPASS_DELETE_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_USERPASS_DELETE_RESPONSE_SCHEMA,
        "tags": ["write", "destructive", "auth", "identity"],
        "safety_level": "dangerous",
        "llm_instructions": VAULT_AUTH_USERPASS_DELETE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.approle.write",
        "handler": vault_auth_approle_write,
        "summary": "Create or update an AppRole's token/SecretID config.",
        "description": (
            "Creates a new AppRole or updates an existing one's token and "
            "SecretID policy/TTL configuration (POST "
            "/v1/auth/<mount>/role/<role>). Binding token_policies is a "
            "privilege assignment -- safety_level=dangerous, "
            "requires_approval=True. Mints no SecretID (that is "
            "generate_secret_id). approle not enabled raises "
            "VaultAuthBackendNotMountedError."
        ),
        "parameter_schema": VAULT_AUTH_APPROLE_WRITE_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_APPROLE_WRITE_RESPONSE_SCHEMA,
        "tags": ["write", "auth", "identity"],
        "safety_level": "dangerous",
        "llm_instructions": VAULT_AUTH_APPROLE_WRITE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.approle.delete",
        "handler": vault_auth_approle_delete,
        "summary": "Delete an AppRole.",
        "description": (
            "Deletes an AppRole, invalidating its role-id and all issued "
            "SecretIDs (DELETE /v1/auth/<mount>/role/<role>). Irreversible "
            "removal of a machine-login identity -- safety_level=dangerous, "
            "requires_approval=True. Idempotent (deleting a non-existent "
            "role is a no-op success), so a 404 only means approle is not "
            "enabled -> VaultAuthBackendNotMountedError."
        ),
        "parameter_schema": VAULT_AUTH_APPROLE_DELETE_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_APPROLE_DELETE_RESPONSE_SCHEMA,
        "tags": ["write", "destructive", "auth", "identity"],
        "safety_level": "dangerous",
        "llm_instructions": VAULT_AUTH_APPROLE_DELETE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.approle.generate_secret_id",
        "handler": vault_auth_approle_generate_secret_id,
        "summary": "Mint a fresh SecretID for an AppRole (non-idempotent).",
        "description": (
            "Mints a brand-new SecretID for an AppRole so a machine "
            "identity can log in (POST "
            "/v1/auth/<mount>/role/<role>/secret-id). NON-IDEMPOTENT: each "
            "call yields a distinct SecretID. The SecretID lands in the "
            "response, so the op is classified credential_mint: the "
            "broadcast collapses to aggregate-only and the audit row holds "
            "only a params_hash, so the minted SecretID never reaches the "
            "feed or audit store. The caller's OperationResult still "
            "carries it -- store it immediately. safety_level=dangerous, "
            "requires_approval=True. approle not enabled raises "
            "VaultAuthBackendNotMountedError; a missing role under a "
            "mounted backend surfaces as InvalidPath."
        ),
        "parameter_schema": VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_RESPONSE_SCHEMA,
        "tags": ["write", "credential-mint", "auth", "identity"],
        "safety_level": "dangerous",
        "llm_instructions": VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_LLM_INSTRUCTIONS,
    },
)


async def register_vault_auth_write_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the six Vault auth-write ops into ``endpoint_descriptor``.

    Called from
    :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
    so the package keeps a single lifespan-driven registrar entry while
    the auth-write surface lives in its own reviewable module. Idempotent:
    a second call against unchanged descriptions is a no-op for the
    embedding pipeline via the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`.

    Every op registers from :data:`_AUTH_WRITE_OP_SPECS` with
    ``group_key="auth"`` and ``requires_approval=True`` -- credential
    lifecycle writes (privilege assignment, identity removal, secret
    mint).
    """
    auth_write_when_to_use = (
        "Use to MUTATE the userpass and approle auth backends -- the "
        "credential lifecycle: create/update a userpass user "
        "(``vault.auth.userpass.write``), rotate a user's password "
        "(``vault.auth.userpass.update_password``), delete a user "
        "(``vault.auth.userpass.delete``), create/update an AppRole "
        "(``vault.auth.approle.write``), delete an AppRole "
        "(``vault.auth.approle.delete``), and mint a SecretID for an "
        "AppRole (``vault.auth.approle.generate_secret_id``). Every op "
        "requires approval. Passwords and minted SecretIDs are secret "
        "material -- redacted from the audit row and the broadcast feed. "
        "Pair with the read side of this group "
        "(``vault.auth.{userpass,approle}.{list,read}``) to inspect "
        "before mutating. Route token-identity and auth-mount questions "
        "to the 'sys' group."
    )
    for spec in _AUTH_WRITE_OP_SPECS:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=spec["op_id"],
            handler=spec["handler"],
            summary=spec["summary"],
            description=spec["description"],
            parameter_schema=spec["parameter_schema"],
            response_schema=spec["response_schema"],
            group_key="auth",
            when_to_use=auth_write_when_to_use,
            tags=spec["tags"],
            safety_level=spec["safety_level"],
            requires_approval=True,
            llm_instructions=spec["llm_instructions"],
            embedding_service=embedding_service,
        )
