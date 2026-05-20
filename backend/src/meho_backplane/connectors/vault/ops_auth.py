# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault identity-read typed-op handlers + ``endpoint_descriptor`` registration.

Op-id namespace (v0.2 G3.3-T3): ``vault.auth.<backend>.<verb>``.

This module ships the **read-only identity surface** the consumer's
``scripts/vault.sh`` wrappers exercise when an operator inspects who can
authenticate to Vault:

* ``vault.auth.userpass.list`` -- ``LIST /v1/auth/<mount>/users``
* ``vault.auth.userpass.read`` -- ``GET  /v1/auth/<mount>/users/<user>``
* ``vault.auth.approle.list``  -- ``LIST /v1/auth/<mount>/role``
* ``vault.auth.approle.read``  -- ``GET  /v1/auth/<mount>/role/<role>``

AppRole **secret-id generation** and every userpass/approle **write**
(create/update/delete user or role) are explicitly out of scope for
v0.2 -- secret-id generation is a high-risk write with policy
implications, deferred to v0.2.next behind a policy gate (Initiative
#366, Task #547 out-of-scope sections). Only the four read ops above
land here.

Handler shape mirrors :mod:`meho_backplane.connectors.vault.ops`
verbatim: each handler is a module-level ``async def`` with the
``(target, params) -> dict[str, Any]`` contract the G0.6 dispatcher
expects from a typed op, and each **raises** on failure rather than
returning a structured ``OperationResult`` -- the dispatcher's
``connector_error`` branch catches the exception and records its class
name in ``extras["exception_class"]`` so callers can distinguish the
failure mode without importing connector internals.

Mount-path parameterisation: hvac's userpass/approle methods take a
``mount_point`` argument that defaults to ``"userpass"`` / ``"approle"``.
Each op's ``parameter_schema`` (in
:mod:`meho_backplane.connectors.vault.ops_auth_schemas`) exposes an
optional ``mount`` property (same default) so a non-default mount
(``auth/userpass-prod``) works without a code change; the handler
forwards it to ``mount_point``.

Auth-backend-not-mounted handling: when the auth method is not enabled
at the requested mount, Vault returns ``404`` and hvac raises
:class:`hvac.exceptions.InvalidPath`. A bare ``InvalidPath`` is
ambiguous -- it is also what Vault returns for a missing *user* or
*role* under a mounted backend. The handlers translate the
backend-absent case into :class:`VaultAuthBackendNotMountedError` (a
:class:`~meho_backplane.auth.vault.VaultClientError` subclass) so the
dispatcher's ``connector_error`` payload carries an operator-actionable
``exception_class`` the consumer's wrappers can branch on, exactly as
the KV-read handler distinguishes login-phase from read-phase failures
via the ``VaultClientError`` hierarchy. A missing user/role under a
*mounted* backend keeps surfacing as the underlying ``InvalidPath`` so
the two not-found shapes stay distinguishable.

The ``_auth_vault`` module reference (not a ``from ... import`` binding)
is used so the existing test seam -- ``monkeypatch.setattr(vault_module,
"_build_client", fake)`` driving
:func:`~meho_backplane.auth.vault.vault_client_for_operator` -- applies
to these handlers transparently, with no respx/httpx layer: hvac's
transport is ``requests``, so the canonical Vault unit-test seam in
this codebase is the in-process fake hvac client, not an httpx mock.

The JSON Schema + ``llm_instructions`` constants live in
:mod:`meho_backplane.connectors.vault.ops_auth_schemas` to keep this
module focused on behaviour and both modules within the file-size
budget. They are re-exported here so existing import sites
(``from ...ops_auth import VAULT_AUTH_USERPASS_LIST_PARAMETER_SCHEMA``)
keep working.
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac.exceptions

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors.vault.ops_auth_schemas import (
    VAULT_AUTH_APPROLE_LIST_LLM_INSTRUCTIONS,
    VAULT_AUTH_APPROLE_LIST_PARAMETER_SCHEMA,
    VAULT_AUTH_APPROLE_LIST_RESPONSE_SCHEMA,
    VAULT_AUTH_APPROLE_READ_LLM_INSTRUCTIONS,
    VAULT_AUTH_APPROLE_READ_PARAMETER_SCHEMA,
    VAULT_AUTH_APPROLE_READ_RESPONSE_SCHEMA,
    VAULT_AUTH_USERPASS_LIST_LLM_INSTRUCTIONS,
    VAULT_AUTH_USERPASS_LIST_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_LIST_RESPONSE_SCHEMA,
    VAULT_AUTH_USERPASS_READ_LLM_INSTRUCTIONS,
    VAULT_AUTH_USERPASS_READ_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_READ_RESPONSE_SCHEMA,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VAULT_AUTH_APPROLE_LIST_PARAMETER_SCHEMA",
    "VAULT_AUTH_APPROLE_READ_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_LIST_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_READ_PARAMETER_SCHEMA",
    "VaultAuthBackendNotMountedError",
    "register_vault_auth_operations",
    "vault_auth_approle_list",
    "vault_auth_approle_read",
    "vault_auth_userpass_list",
    "vault_auth_userpass_read",
]


class VaultAuthBackendNotMountedError(VaultClientError):
    """The requested auth backend is not enabled at the given mount path.

    Raised by the identity-read handlers when Vault returns ``404`` for
    a ``LIST``/``GET`` against ``auth/<mount>/...`` *because the auth
    method itself is not mounted* (as opposed to a missing user/role
    under a mounted backend, which keeps surfacing as the underlying
    :class:`hvac.exceptions.InvalidPath`).

    Subclasses :class:`~meho_backplane.auth.vault.VaultClientError` so a
    caller that already catches the Vault error base class for the
    KV-read path gets the same single error-response shape, and the
    dispatcher's ``connector_error`` branch records
    ``exception_class="VaultAuthBackendNotMountedError"`` -- an
    operator-actionable signal ("enable the auth method or fix the
    mount path") distinct from a transient read failure.
    """


# --- handlers --------------------------------------------------------------


async def vault_auth_userpass_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List usernames on a Vault userpass auth mount.

    Op-id: ``vault.auth.userpass.list``. Delegates to hvac's
    ``client.auth.userpass.list_user(mount_point=...)``
    (``LIST /v1/auth/<mount>/users``) and returns the unwrapped
    ``data`` dict, normalised to always carry a ``keys`` list.

    Raises
    ------
    VaultAuthBackendNotMountedError
        userpass is not enabled at ``mount`` (Vault ``404``).
    meho_backplane.auth.vault.VaultClientError
        Login-side failure (Vault unreachable, role denied).
    Exception
        Any other hvac-side error; the dispatcher's ``connector_error``
        branch surfaces ``extras["exception_class"]``.
    """
    mount: str = str(params.get("mount", "userpass")).strip()
    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            payload = await asyncio.to_thread(client.auth.userpass.list_user, mount_point=mount)
        except hvac.exceptions.InvalidPath as exc:
            raise VaultAuthBackendNotMountedError(
                f"userpass auth backend not mounted at {mount!r}"
            ) from exc
        return {"keys": _extract_keys(payload)}


async def vault_auth_userpass_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one userpass user's config (policies, token TTLs).

    Op-id: ``vault.auth.userpass.read``. Delegates to hvac's
    ``client.auth.userpass.read_user(username=..., mount_point=...)``
    (``GET /v1/auth/<mount>/users/<user>``) and returns the unwrapped
    ``data`` config dict.

    A missing user under a *mounted* backend keeps surfacing as the
    underlying :class:`hvac.exceptions.InvalidPath` (Vault ``404``);
    only the backend-absent case is translated to
    :class:`VaultAuthBackendNotMountedError`. The two are
    indistinguishable from a single ``404`` on the read path, so the
    handler probes the mount with a ``list_user`` call before
    reclassifying -- if the list succeeds the backend is mounted and
    the original ``InvalidPath`` (missing user) is re-raised.
    """
    username: str = str(params["username"]).strip()
    mount: str = str(params.get("mount", "userpass")).strip()
    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            payload = await asyncio.to_thread(
                client.auth.userpass.read_user,
                username=username,
                mount_point=mount,
            )
        except hvac.exceptions.InvalidPath as exc:
            await _reclassify_not_found(client, exc, mount=mount, backend="userpass")
        return _extract_data(payload)


async def vault_auth_approle_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List role names on a Vault approle auth mount.

    Op-id: ``vault.auth.approle.list``. Delegates to hvac's
    ``client.auth.approle.list_roles(mount_point=...)``
    (``LIST /v1/auth/<mount>/role``) and returns the unwrapped ``data``
    dict, normalised to always carry a ``keys`` list.

    Raises
    ------
    VaultAuthBackendNotMountedError
        approle is not enabled at ``mount`` (Vault ``404``).
    meho_backplane.auth.vault.VaultClientError
        Login-side failure (Vault unreachable, role denied).
    """
    mount: str = str(params.get("mount", "approle")).strip()
    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            payload = await asyncio.to_thread(client.auth.approle.list_roles, mount_point=mount)
        except hvac.exceptions.InvalidPath as exc:
            raise VaultAuthBackendNotMountedError(
                f"approle auth backend not mounted at {mount!r}"
            ) from exc
        return {"keys": _extract_keys(payload)}


async def vault_auth_approle_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one AppRole's config (policies, token + secret-id TTLs).

    Op-id: ``vault.auth.approle.read``. Delegates to hvac's
    ``client.auth.approle.read_role(role_name=..., mount_point=...)``
    (``GET /v1/auth/<mount>/role/<role>``) and returns the unwrapped
    ``data`` config dict. Never returns or generates a secret-id --
    secret-id generation is out of scope for v0.2.

    A missing role under a *mounted* backend keeps surfacing as the
    underlying :class:`hvac.exceptions.InvalidPath`; only the
    backend-absent case is translated to
    :class:`VaultAuthBackendNotMountedError`.
    """
    role_name: str = str(params["role_name"]).strip()
    mount: str = str(params.get("mount", "approle")).strip()
    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            payload = await asyncio.to_thread(
                client.auth.approle.read_role,
                role_name=role_name,
                mount_point=mount,
            )
        except hvac.exceptions.InvalidPath as exc:
            await _reclassify_not_found(client, exc, mount=mount, backend="approle")
        return _extract_data(payload)


# --- shared payload helpers ------------------------------------------------


def _extract_data(payload: Any) -> dict[str, Any]:
    """Unwrap Vault's ``{"data": {...}}`` envelope.

    hvac's ``JSONAdapter`` returns the decoded JSON dict for a 200.
    Vault wraps the read payload under ``data``; we surface that inner
    dict verbatim (mirroring ``vault.kv.read``'s structural unwrap).
    Raises :class:`KeyError` on a malformed envelope so the
    dispatcher's ``connector_error`` branch reports
    ``exception_class="KeyError"`` (a read-phase failure, distinct from
    the login-phase ``VaultClientError`` subclasses).
    """
    return dict(payload["data"])


def _extract_keys(payload: Any) -> list[str]:
    """Unwrap a Vault LIST ``{"data": {"keys": [...]}}`` envelope.

    A LIST against an empty (but mounted) backend can return ``204`` --
    hvac surfaces that as a falsy payload. Normalise to ``[]`` so the
    op's contract ('always a list') holds regardless of whether the
    backend has zero or many entries.
    """
    if not payload:
        return []
    data = payload.get("data") or {}
    keys = data.get("keys") or []
    return list(keys)


async def _reclassify_not_found(
    client: Any,
    exc: hvac.exceptions.InvalidPath,
    *,
    mount: str,
    backend: str,
) -> None:
    """Re-raise a read-path ``404`` as backend-absent xor missing-entity.

    A single ``GET`` ``404`` cannot tell "auth method not mounted" from
    "method mounted, entity missing". Probe the mount with the cheap
    LIST endpoint: if the LIST succeeds (or raises anything other than
    ``InvalidPath``) the backend is mounted, so the original
    ``InvalidPath`` (missing user/role) is re-raised verbatim. If the
    LIST itself ``404``s, the backend is not mounted -- raise
    :class:`VaultAuthBackendNotMountedError`.

    Always raises (never returns); typed as ``-> None`` because the
    caller treats it as a re-raise point.
    """
    list_fn = (
        client.auth.userpass.list_user if backend == "userpass" else client.auth.approle.list_roles
    )
    try:
        await asyncio.to_thread(list_fn, mount_point=mount)
    except hvac.exceptions.InvalidPath as list_exc:
        raise VaultAuthBackendNotMountedError(
            f"{backend} auth backend not mounted at {mount!r}"
        ) from list_exc
    # LIST succeeded -> backend is mounted; the original 404 was a
    # missing user/role. Re-raise it so callers see ``InvalidPath``.
    raise exc


# --- registration ----------------------------------------------------------

#: Per-op registration spec. Collapsing the four near-identical
#: ``register_typed_operation`` calls into a data table keeps the
#: registrar small and makes the surface auditable at a glance: every
#: row is ``safety_level="safe"``, ``group_key="auth"``,
#: ``requires_approval=False`` (read-only identity inspection, the
#: lowest-risk class). ``op_class=read`` is carried via the
#: ``"read-only"`` tag -- ``endpoint_descriptor`` has no dedicated
#: ``op_class`` column in v0.2; the dispatcher's audit writer derives
#: ``op_class=read`` for non-write safety levels and the meta-tools
#: read the tag.
_AUTH_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": "vault.auth.userpass.list",
        "handler": vault_auth_userpass_list,
        "summary": "List usernames on a Vault userpass auth mount.",
        "description": (
            "Lists every configured username on the userpass auth "
            "backend (LIST /v1/auth/<mount>/users). Mount path is "
            "parameterised (default 'userpass'). Read-only -- never "
            "mutates the auth backend. Returns {'keys': [...]}; an "
            "empty mount yields an empty list. userpass not enabled at "
            "the mount raises VaultAuthBackendNotMountedError, surfaced "
            "by the dispatcher as a connector_error OperationResult."
        ),
        "parameter_schema": VAULT_AUTH_USERPASS_LIST_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_USERPASS_LIST_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_AUTH_USERPASS_LIST_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.userpass.read",
        "handler": vault_auth_userpass_read,
        "summary": "Read one userpass user's policies and token TTLs.",
        "description": (
            "Reads a single userpass user's configuration (GET "
            "/v1/auth/<mount>/users/<user>): token_policies, token_ttl, "
            "token_max_ttl, token_bound_cidrs, token_type. Mount path "
            "is parameterised (default 'userpass'). Read-only. Never "
            "returns the password (Vault does not expose it). userpass "
            "not enabled raises VaultAuthBackendNotMountedError; a "
            "missing user under a mounted backend surfaces as "
            "InvalidPath -- both land as a connector_error "
            "OperationResult with the class in extras.exception_class."
        ),
        "parameter_schema": VAULT_AUTH_USERPASS_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_USERPASS_READ_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_AUTH_USERPASS_READ_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.approle.list",
        "handler": vault_auth_approle_list,
        "summary": "List role names on a Vault approle auth mount.",
        "description": (
            "Lists every AppRole role name (LIST "
            "/v1/auth/<mount>/role). Mount path is parameterised "
            "(default 'approle'). Read-only -- never mutates the auth "
            "backend and never generates a secret-id (out of scope for "
            "v0.2). Returns {'keys': [...]}; an empty mount yields an "
            "empty list. approle not enabled raises "
            "VaultAuthBackendNotMountedError."
        ),
        "parameter_schema": VAULT_AUTH_APPROLE_LIST_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_APPROLE_LIST_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_AUTH_APPROLE_LIST_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.auth.approle.read",
        "handler": vault_auth_approle_read,
        "summary": "Read one AppRole's policies, token and secret-id TTLs.",
        "description": (
            "Reads a single AppRole's configuration (GET "
            "/v1/auth/<mount>/role/<role>): token_policies, token_ttl, "
            "token_max_ttl, secret_id_ttl, secret_id_num_uses, "
            "bind_secret_id. Mount path is parameterised (default "
            "'approle'). Read-only -- returns config only, never a "
            "secret-id (generation is out of scope for v0.2). approle "
            "not enabled raises VaultAuthBackendNotMountedError; a "
            "missing role under a mounted backend surfaces as "
            "InvalidPath."
        ),
        "parameter_schema": VAULT_AUTH_APPROLE_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_APPROLE_READ_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_AUTH_APPROLE_READ_LLM_INSTRUCTIONS,
    },
)


async def register_vault_auth_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the four Vault identity-read ops into ``endpoint_descriptor``.

    Called from
    :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
    so the package keeps a single lifespan-driven registrar entry while
    the auth read surface lives in its own reviewable module (Task
    #547). Idempotent: a second call against unchanged descriptions is
    a no-op for the embedding pipeline via the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`.

    Every op registers from :data:`_AUTH_OP_SPECS` with
    ``safety_level="safe"``, ``group_key="auth"`` and
    ``requires_approval=False`` -- read-only identity inspection.
    """
    for spec in _AUTH_OP_SPECS:
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
            # G0.9-T4a #731 placeholder; T4b #732 replaces with a
            # curated blurb for the ``auth`` group.
            when_to_use="TODO: curate (T4b #732)",
            tags=["read-only", "auth", "identity"],
            safety_level="safe",
            requires_approval=False,
            llm_instructions=spec["llm_instructions"],
            embedding_service=embedding_service,
        )
