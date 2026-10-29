# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.token.create_and_store``: make a Vault token and store it, never show it.

``vault.token.create`` returns the new token to the caller. When the token
must end up in a Vault KV path or in a Kubernetes Secret, someone then has
to copy it there by hand. This op does the whole job inside MEHO: it makes
the token and writes it to the place the caller names. The token is never
a parameter, never in the result, the audit row, a log line or an error.

Steps, in this order:

1. **Check the destination before the token exists.**

   * ``vault_kv``: the path shape and the tenant check
     (:func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`).
   * ``k8s_secret``: the target exists in the caller's tenant and is a
     Kubernetes target; the caller's MEHO policy allows ``k8s.secret.update``
     on it (the dispatcher's own ``policy_gate``); the Secret exists and is
     not immutable (only its key names are used).

2. **Make the token** under this Vault target's role
   (:func:`~meho_backplane.connectors.vault.target_auth.vault_client_for_target`):
   ``POST auth/token/create/<role_name>``, or ``auth/token/create-orphan``
   without a role.
3. **Check that it is an orphan** (a token with no parent). MEHO's own Vault
   login token is revoked when the login ends, and Vault revokes every
   child token with it. So a token with a parent would die right after the
   call. When Vault answers ``orphan: false`` MEHO revokes the token at once
   and stops; the error says the role must set ``orphan=true``.
4. **Write the token** by calling the existing handlers directly: the
   ``vault.kv.patch`` handler (``vault.kv.put`` when the path is new), or the
   ``k8s.secret.update`` handler in ``patch`` mode. No second operation is
   started, so the token never sits in any operation's parameters.
5. **Clean up on failure.** When anything fails after step 2, also a
   cancellation and also while MEHO's own Vault login ends, MEHO revokes
   the new token (shielded from cancellation) with the token itself
   (``auth/token/revoke-self`` is in Vault's ``default`` policy). When that
   revoke fails too, the error names the accessor and
   ``vault.token.revoke_accessor``.
6. **Return** ``{accessor, expire_time, ttl_seconds, policies, stored_to}``.

The op is pinned ``credential_mint``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_MINT_OPS`): the
broadcast is aggregate-only and the flight recorder keeps no bodies. Its
tags mark it as secret-bearing, so the dispatcher replaces the text of a
raised error with a fixed text; every error here therefore carries its
MEHO-written text as ``remediation`` too.

At approval time a preview builder shows the token settings and the
destination, and a permission preflight asks Vault
(``sys/capabilities-self``) whether this target's role may make the token
and, for a ``vault_kv`` store, write the KV path. When it may not, MEHO
does not ask for approval (``write_capability_denied``). Both live in
:mod:`meho_backplane.connectors.vault.ops_token_store_preview`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import hvac.exceptions
import structlog

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault import ops as _kv_ops
from meho_backplane.connectors.vault.ops_token_store_preview import (
    TOKEN_STORE_OP,
    kv_destination,
)
from meho_backplane.connectors.vault.ops_token_store_schemas import (
    VAULT_TOKEN_CREATE_AND_STORE_LLM_INSTRUCTIONS,
    VAULT_TOKEN_CREATE_AND_STORE_PARAMETER_SCHEMA,
    VAULT_TOKEN_CREATE_AND_STORE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.connectors.vault.tenant_scope import (
    VaultTenantScopeError,
    enforce_tenant_scope,
)

__all__ = [
    "TOKEN_STORE_OP",
    "TOKEN_STORE_OP_SPECS",
    "TokenStoreError",
    "vault_token_create_and_store",
]

_log = structlog.get_logger(__name__)

_K8S_UPDATE_OP: Final = "k8s.secret.update"
_K8S_CONNECTOR_ID: Final = "k8s-1.x"
_CONTEXT_TARGET_KEYS: Final = ("target_id", "target_name")

#: Token settings forwarded to hvac when the caller gives them.
#: ``ttl_period`` maps onto hvac's ``period=`` separately.
_CREATE_FIELDS: Final = (
    "policies",
    "ttl",
    "explicit_max_ttl",
    "num_uses",
    "display_name",
    "renewable",
)


class TokenStoreError(Exception):
    """The op stopped. The message is MEHO's own text and never holds the token.

    The same text is ``remediation``, so the caller still reads it when the
    dispatcher withholds error messages of a secret-bearing op.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.remediation = message


@dataclass
class _Minted:
    """The new token. ``repr`` never shows the token."""

    token: str = field(repr=False)
    accessor: str
    orphan: bool
    ttl_seconds: int
    policies: list[str]


# ---------------------------------------------------------------------------
# Step 1: the destination
# ---------------------------------------------------------------------------


def _checkkv_destination(operator: Operator, store: Mapping[str, Any]) -> None:
    """Path shape + tenant check for a ``vault_kv`` store, before any token exists."""
    mount, path, _field = kv_destination(store)
    try:
        _kv_ops._extract_mount_and_path("vault.kv.patch", {"mount": mount, "path": path})
        enforce_tenant_scope(operator, mount=mount, path=path, read_only=False)
    except (VaultTenantScopeError, _kv_ops.VaultPathShapeError) as exc:
        raise TokenStoreError(f"{exc} No token was made.") from None


@dataclass
class _K8sDestination:
    """A checked ``k8s_secret`` store: the resolved target and its connector."""

    target: Any
    connector: Any
    namespace: str
    name: str
    key: str


async def _resolve_k8s_target(operator: Operator, name: str) -> tuple[Any, Any]:
    """Resolve *name* in the caller's tenant to ``(target, connector instance)``.

    The target resolver binds ``target_id`` / ``target_name`` into the
    structlog context for the audit middleware; they are put back afterwards
    so this op's audit row stays on the Vault target.
    """
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.resolver import resolve_connector_or_label
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
    from meho_backplane.targets.resolver import (
        AmbiguousTargetError,
        TargetNotFoundError,
        resolve_target,
    )

    bound = structlog.contextvars.get_contextvars()
    saved = {k: bound[k] for k in _CONTEXT_TARGET_KEYS if k in bound}
    try:
        async with get_sessionmaker()() as session:
            target = await resolve_target(session, operator.tenant_id, name)
    except TargetNotFoundError:
        raise TokenStoreError(
            f"store.k8s_secret.target {name!r}: no such target in your tenant "
            "(list_targets). No token was made."
        ) from None
    except AmbiguousTargetError:
        raise TokenStoreError(
            f"store.k8s_secret.target {name!r} is ambiguous (an alias shared by "
            "several targets); use the target's name. No token was made."
        ) from None
    finally:
        structlog.contextvars.unbind_contextvars(*_CONTEXT_TARGET_KEYS)
        structlog.contextvars.bind_contextvars(**saved)
    cls, _label, _problem = resolve_connector_or_label(target)
    if cls is None or not issubclass(cls, KubernetesConnector):
        raise TokenStoreError(
            f"store.k8s_secret.target {name!r} is a {getattr(target, 'product', None)!r} "
            "target, not a Kubernetes target. No token was made."
        )
    return target, get_or_create_connector_instance(cls)


async def _k8s_policy_refusal(operator: Operator, k8s_target: Any) -> str | None:
    """Run the policy gate for ``k8s.secret.update`` on *k8s_target*; ``None`` = allowed.

    The write calls the ``k8s.secret.update`` handler directly, so the
    per-(principal, op, target) policy a dispatched ``k8s.secret.update``
    gets would be skipped without this check. ``needs-approval`` is
    satisfied by this op's own approval: it is allowed only on the approved
    run (the dispatcher binds ``policy_decision_var`` to ``needs-approval``
    there). A ``deny`` refuses.
    """
    from meho_backplane.db.models import PermissionVerdict
    from meho_backplane.operations._audit import policy_decision_var
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id
    from meho_backplane.operations._validate import policy_gate

    where = f"{_K8S_UPDATE_OP} on target {k8s_target.name!r}"
    product, version, impl_id = parse_connector_id(_K8S_CONNECTOR_ID)
    descriptor = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=_K8S_UPDATE_OP,
    )
    if descriptor is None:
        return f"policy_denied: {where}: op not registered, its policy cannot be evaluated"
    verdict, reason = await policy_gate(
        operator=operator,
        descriptor=descriptor,
        target=k8s_target,
        connector_id=_K8S_CONNECTOR_ID,
    )
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        return None
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        if policy_decision_var.get() == PermissionVerdict.NEEDS_APPROVAL.value:
            return None
        return (
            f"policy_denied: {where} needs approval ({reason}), but this run was not "
            "approved -- call the operation so it waits for approval"
        )
    return f"policy_denied: {where}: {reason or 'denied'}"


async def _check_k8s_destination(operator: Operator, store: Mapping[str, Any]) -> _K8sDestination:
    """Target, MEHO policy and Secret checks for a ``k8s_secret`` store."""
    from kubernetes_asyncio import client

    from meho_backplane.connectors.kubernetes.ops_secret_write import (
        KubernetesSecretNotFoundError,
        read_secret_or_raise,
    )

    target, connector = await _resolve_k8s_target(operator, str(store["target"]))
    dest = _K8sDestination(
        target, connector, str(store["namespace"]), str(store["name"]), str(store["key"])
    )
    refusal = await _k8s_policy_refusal(operator, dest.target)
    if refusal is not None:
        raise TokenStoreError(f"{refusal}. No token was made.")
    api_client = await dest.connector._get_api_client(dest.target, operator)
    core_v1 = client.CoreV1Api(api_client)
    try:
        secret = await read_secret_or_raise(core_v1, name=dest.name, namespace=dest.namespace)
    except KubernetesSecretNotFoundError as exc:
        raise TokenStoreError(f"{exc} No token was made.") from None
    if getattr(secret, "immutable", None):
        raise TokenStoreError(
            f"Secret {dest.namespace}/{dest.name} is immutable, so its values cannot "
            "change. No token was made."
        )
    return dest


# ---------------------------------------------------------------------------
# Steps 2, 3 and 5: make the token, check it, revoke it
# ---------------------------------------------------------------------------


def _create_kwargs(params: Mapping[str, Any]) -> dict[str, Any]:
    kwargs = {name: params[name] for name in _CREATE_FIELDS if params.get(name) is not None}
    if params.get("ttl_period") is not None:
        kwargs["period"] = params["ttl_period"]
    return kwargs


def _minted_from(payload: Any) -> _Minted:
    auth = payload.get("auth") if isinstance(payload, Mapping) else None
    if not isinstance(auth, Mapping) or not auth.get("client_token"):
        raise TokenStoreError(f"{TOKEN_STORE_OP}: Vault's answer held no token")
    return _Minted(
        token=str(auth["client_token"]),
        accessor=str(auth.get("accessor") or ""),
        orphan=auth.get("orphan") is True,
        ttl_seconds=int(auth.get("lease_duration") or 0),
        policies=[str(p) for p in auth.get("policies") or []],
    )


async def _revoke(minted: _Minted) -> bool:
    """Revoke the new token with the token itself (``revoke-self``); never raises."""
    from meho_backplane.settings import get_settings

    def _do() -> None:
        client = _auth_vault._build_client(get_settings(), token=minted.token)
        client.auth.token.revoke_self()

    try:
        await asyncio.to_thread(_do)
    except Exception as exc:
        _log.warning(
            "vault_token_store_revoke_failed",
            accessor=minted.accessor,
            error=type(exc).__name__,
        )
        return False
    return True


def _revoke_outcome(minted: _Minted, revoked: bool) -> str:
    if revoked:
        return "MEHO revoked the new token"
    return (
        f"MEHO could NOT revoke the new token (accessor {minted.accessor}) -- revoke it "
        f"with vault.token.revoke_accessor, accessor={minted.accessor!r}"
    )


async def _mint(operator: Operator, target: Any, params: Mapping[str, Any]) -> _Minted:
    """Make the token under the target's Vault role; refuse and revoke a non-orphan.

    The orphan check and its revoke run while MEHO's own login is still
    open: a non-orphan token is a child of that login, and Vault revokes it
    with the login when the block ends.

    Ending the login block is a network call too. When it fails or is
    cancelled after an orphan token exists, MEHO revokes that token here
    (shielded from cancellation), because the caller's clean-up only starts
    after this function returns.
    """
    role_name = params.get("role_name")
    kwargs = _create_kwargs(params)
    minted: _Minted | None = None
    try:
        async with vault_client_for_target(operator, target) as client:
            if role_name:
                payload = await asyncio.to_thread(
                    client.auth.token.create, role_name=role_name, **kwargs
                )
            else:
                payload = await asyncio.to_thread(client.auth.token.create_orphan, **kwargs)
            minted = _minted_from(payload)
            if not minted.orphan:
                revoked = await asyncio.shield(_revoke(minted))
                raise TokenStoreError(
                    f"{TOKEN_STORE_OP}: Vault made a token with a parent, so it would die when "
                    f"MEHO's own login ends; {_revoke_outcome(minted, revoked)}. The token "
                    f"role must set orphan=true: vault.token.role.write "
                    f"role_name={role_name!r} orphan=true."
                )
    except BaseException:
        if minted is not None and minted.orphan:
            revoked = await asyncio.shield(_revoke(minted))
            _log.warning("vault_token_store_aborted", accessor=minted.accessor, revoked=revoked)
        raise
    return minted


# ---------------------------------------------------------------------------
# Step 4: write it
# ---------------------------------------------------------------------------


async def _store_in_kv(
    operator: Operator, target: Any, store: Mapping[str, Any], token: str
) -> dict[str, Any]:
    mount, path, field_name = kv_destination(store)
    kv_params = {"mount": mount, "path": path, "data": {field_name: token}}
    try:
        written = await _kv_ops.vault_kv_patch(operator, target, kv_params)
    except hvac.exceptions.InvalidPath:
        written = await _kv_ops.vault_kv_put(operator, target, kv_params)
    return {
        "kind": "vault_kv",
        "mount": mount,
        "path": path,
        "field": field_name,
        "version": written.get("version"),
    }


async def _store_in_k8s(operator: Operator, dest: _K8sDestination, token: str) -> dict[str, Any]:
    from meho_backplane.connectors.kubernetes.ops_secret_write import k8s_secret_update

    await k8s_secret_update(
        dest.connector,
        dest.target,
        operator,
        {
            "name": dest.name,
            "namespace": dest.namespace,
            "mode": "patch",
            "string_data": {dest.key: token},
        },
    )
    return {
        "kind": "k8s_secret",
        "target": dest.target.name,
        "namespace": dest.namespace,
        "name": dest.name,
        "key": dest.key,
    }


def _where(store: Mapping[str, Any]) -> str:
    if "vault_kv" in store:
        mount, path, field_name = kv_destination(store["vault_kv"])
        return f"Vault KV {mount}/{path} (field {field_name!r})"
    k8s = store["k8s_secret"]
    return f"Secret {k8s['namespace']}/{k8s['name']} key {k8s['key']!r} on {k8s['target']!r}"


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def _result(minted: _Minted, stored_to: dict[str, Any], created_at: datetime) -> dict[str, Any]:
    expire_time = None
    if minted.ttl_seconds > 0:
        expire_time = (created_at + timedelta(seconds=minted.ttl_seconds)).isoformat()
    return {
        "accessor": minted.accessor,
        "expire_time": expire_time,
        "ttl_seconds": minted.ttl_seconds,
        "policies": minted.policies,
        "stored_to": stored_to,
    }


async def vault_token_create_and_store(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Make an orphan Vault token and write it to the named place; return no token.

    Op-id: ``vault.token.create_and_store``. See the module docstring for
    the steps and the no-show guarantee.
    """
    store: Mapping[str, Any] = params["store"]
    k8s_dest: _K8sDestination | None = None
    if "vault_kv" in store:
        _checkkv_destination(operator, store["vault_kv"])
    else:
        k8s_dest = await _check_k8s_destination(operator, store["k8s_secret"])

    created_at = datetime.now(UTC)
    minted = await _mint(operator, target, params)
    try:
        if k8s_dest is None:
            stored_to = await _store_in_kv(operator, target, store["vault_kv"], minted.token)
        else:
            stored_to = await _store_in_k8s(operator, k8s_dest, minted.token)
    except BaseException as exc:
        revoked = await asyncio.shield(_revoke(minted))
        if not isinstance(exc, Exception):
            _log.warning("vault_token_store_aborted", accessor=minted.accessor, revoked=revoked)
            raise
        raise TokenStoreError(
            f"{TOKEN_STORE_OP}: the new token could not be written to {_where(store)} "
            f"({type(exc).__name__}); {_revoke_outcome(minted, revoked)}. Check that the "
            "write is allowed there and call the operation again."
        ) from None
    return _result(minted, stored_to, created_at)


# --- spec table ------------------------------------------------------------

#: Registration row for the op. ``group_key`` is ``token``. Consumed by the
#: package composer ``ops_identity_token.register_vault_identity_token_operations``.
TOKEN_STORE_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": TOKEN_STORE_OP,
        "handler": vault_token_create_and_store,
        "group_key": "token",
        "summary": "Make a Vault token from a token role and store it in Vault or a Secret.",
        "description": (
            "Makes an orphan token (POST auth/token/create/<role_name>) and writes it "
            "to a Vault KV path or one key of an existing Kubernetes Secret. The token "
            "is never returned: the result has the accessor, expiry, policies and "
            "where it was stored. Dangerous; needs approval."
        ),
        "parameter_schema": VAULT_TOKEN_CREATE_AND_STORE_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_CREATE_AND_STORE_RESPONSE_SCHEMA,
        "tags": ["write", "credential-mint", "token", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_TOKEN_CREATE_AND_STORE_LLM_INSTRUCTIONS,
    },
)
