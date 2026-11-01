# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault *token* op handlers + spec table (G3.15-T4, #1412).

The token group surfaces the core Vault ``token`` auth backend (always
mounted):

* ``vault.token.create`` makes a client token (``requires_approval=True``).
  MEHO does not show the new token: the result has only its accessor and
  policies. To get a token you can use, call ``vault.token.create_and_store``.
* ``vault.token.revoke_accessor`` surgically revokes a single token by
  accessor (``requires_approval=True``).
* ``vault.token.list_accessors`` lists accessor handles
  (``safety_level="safe"``).
* ``vault.token.lookup_accessor`` reads one token's TTL / policies /
  expiry by its accessor (``safety_level="safe"``).

Token roles (``vault.token.role.write`` / ``.read`` / ``.list``) live in
the sibling :mod:`meho_backplane.connectors.vault.ops_token_role` and
share this group.

There is **no bulk-revoke op** by design -- never revoke broadly to
recover from one leak (the vault skill's loudest Don't-rule). There is
likewise no bulk full-detail lookup: an agent that needs many tokens'
detail lists accessors then looks up each of interest.

**The new token is never returned or stored.** The handler drops
``client_token`` from Vault's answer, so the result, the audit row's raw
copy, the approval result and the run record hold only the accessor and
policies. :func:`meho_backplane.broadcast.events.classify_op` still maps the
op to ``credential_mint`` (aggregate-only broadcast, no flight-recorder
bodies). MEHO makes the token under its own Vault login for this call;
Vault revokes a child token when that login is revoked at the end of the
call. ``vault.token.create_and_store`` makes an orphan token that stays
valid and writes it where the caller says. A token **accessor**
(``revoke_accessor`` / ``lookup_accessor`` param, ``list_accessors``
response) is a reference handle, not the token secret; ``lookup_accessor``'s
response also omits the token ``id`` (Vault blanks it).

Handler shape mirrors :mod:`meho_backplane.connectors.vault.ops_auth`:
``async def (operator, target, params) -> dict``, raises on failure,
opens its Vault client through
:func:`~meho_backplane.connectors.vault.target_auth.vault_client_for_target`
(the operator JWT, logged in under the target's ``extras["vault_role"]``
when set, else the deployment-wide role; a refused target role fails the
call with no fallback), offloads the blocking hvac call with
``asyncio.to_thread``. The ``token`` backend is
core (always mounted); ``list_accessors`` normalises an empty-store
``404`` to ``{"keys": []}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops_auth import _extract_data, _extract_keys
from meho_backplane.connectors.vault.ops_token_schemas import (
    VAULT_TOKEN_CREATE_LLM_INSTRUCTIONS,
    VAULT_TOKEN_CREATE_PARAMETER_SCHEMA,
    VAULT_TOKEN_CREATE_RESPONSE_SCHEMA,
    VAULT_TOKEN_LIST_ACCESSORS_LLM_INSTRUCTIONS,
    VAULT_TOKEN_LIST_ACCESSORS_PARAMETER_SCHEMA,
    VAULT_TOKEN_LIST_ACCESSORS_RESPONSE_SCHEMA,
    VAULT_TOKEN_LOOKUP_ACCESSOR_LLM_INSTRUCTIONS,
    VAULT_TOKEN_LOOKUP_ACCESSOR_PARAMETER_SCHEMA,
    VAULT_TOKEN_LOOKUP_ACCESSOR_RESPONSE_SCHEMA,
    VAULT_TOKEN_REVOKE_ACCESSOR_LLM_INSTRUCTIONS,
    VAULT_TOKEN_REVOKE_ACCESSOR_PARAMETER_SCHEMA,
    VAULT_TOKEN_REVOKE_ACCESSOR_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target

__all__ = [
    "TOKEN_OP_SPECS",
    "TOKEN_WHEN_TO_USE",
    "vault_token_create",
    "vault_token_list_accessors",
    "vault_token_lookup_accessor",
    "vault_token_revoke_accessor",
]


def _hvac_invalid_path() -> type[BaseException]:
    """Return :class:`hvac.exceptions.InvalidPath` (lazy import).

    Kept as a tiny indirection so the ``except`` clause reads uniformly.
    hvac is a hard dependency (imported transitively via ``ops_auth``),
    so this never fails.
    """
    import hvac.exceptions

    invalid_path: type[BaseException] = hvac.exceptions.InvalidPath
    return invalid_path


#: token.create config keys forwarded to hvac verbatim when present.
#: ``ttl_period`` is handled separately (maps onto hvac's ``period=``).
_TOKEN_CREATE_FIELDS: tuple[str, ...] = (
    "policies",
    "ttl",
    "explicit_max_ttl",
    "display_name",
    "num_uses",
    "renewable",
    "no_parent",
    "entity_alias",
)


async def vault_token_create(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Make a Vault client token and return its accessor, never the token.

    Op-id: ``vault.token.create``. Delegates to hvac's
    ``client.auth.token.create(policies=..., ttl=..., ...)``. Each call
    makes a new token, so the op is non-idempotent.

    Vault's answer carries the new ``client_token``. The handler keeps only
    the ``accessor`` (a non-secret handle for ``vault.token.lookup_accessor``
    and ``vault.token.revoke_accessor``) and the ``policies``. So the token
    is in no result, audit row, approval result or run record. To get a
    token you can use, call ``vault.token.create_and_store``.
    """
    kwargs: dict[str, Any] = {}
    for field in _TOKEN_CREATE_FIELDS:
        if field in params and params[field] is not None:
            kwargs[field] = params[field]
    # ``ttl_period`` maps onto hvac's ``period=`` (periodic-token knob);
    # renamed in the schema to avoid clashing with the ``ttl`` field.
    if params.get("ttl_period") is not None:
        kwargs["period"] = params["ttl_period"]

    async with vault_client_for_target(operator, target) as client:
        payload = await asyncio.to_thread(client.auth.token.create, **kwargs)

    auth = dict(payload["auth"])
    result: dict[str, Any] = {"accessor": auth["accessor"]}
    if "policies" in auth:
        result["policies"] = list(auth["policies"])
    return result


async def vault_token_revoke_accessor(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Revoke ONE token by its accessor (surgical; NO bulk-revoke).

    Op-id: ``vault.token.revoke_accessor``. Delegates to hvac's
    ``client.auth.token.revoke_accessor(accessor=...)``. Revokes exactly
    the one token the accessor references -- never a tree, never a bulk
    sweep (there is no bulk-revoke op by design). The accessor is a
    reference handle, not the token secret.
    """
    accessor: str = str(params["accessor"]).strip()

    async with vault_client_for_target(operator, target) as client:
        await asyncio.to_thread(client.auth.token.revoke_accessor, accessor=accessor)

    return {"accessor": accessor, "revoked": True}


async def vault_token_list_accessors(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List every active token's accessor handle.

    Op-id: ``vault.token.list_accessors``. Delegates to hvac's
    ``client.auth.token.list_accessors()`` and returns a normalised
    ``{"keys": [...]}`` dict. Accessors are non-secret references (never
    token secrets). An empty token store normalises to ``{"keys": []}``.
    """
    async with vault_client_for_target(operator, target) as client:
        try:
            payload = await asyncio.to_thread(client.auth.token.list_accessors)
        except _hvac_invalid_path():
            return {"keys": []}

    return {"keys": _extract_keys(payload)}


async def vault_token_lookup_accessor(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Look up ONE token's TTL / policies / expiry by its accessor.

    Op-id: ``vault.token.lookup_accessor``. Delegates to hvac's
    ``client.auth.token.lookup_accessor(accessor=...)`` (POST
    /v1/auth/token/lookup-accessor) and returns the unwrapped ``data``
    metadata dict (policies, ttl, expire_time, period, display_name,
    creation_time, renewable, ...). The accessor is a reference handle,
    not the token secret, and Vault deliberately blanks the token ``id``
    in a lookup-accessor response -- the endpoint exists precisely so this
    is a safe read. Unlike ``list_accessors`` (which normalises an
    empty-store ``404`` to ``{"keys": []}``), a single-accessor lookup has
    no "legitimately empty" case: an unknown accessor is a genuine
    not-found, so the underlying :class:`hvac.exceptions.InvalidPath`
    propagates as a read-phase ``connector_error`` rather than being
    swallowed.
    """
    accessor: str = str(params["accessor"]).strip()

    async with vault_client_for_target(operator, target) as client:
        payload = await asyncio.to_thread(client.auth.token.lookup_accessor, accessor=accessor)

    return _extract_data(payload)


# --- spec table ------------------------------------------------------------

TOKEN_WHEN_TO_USE: str = (
    "Use to manage Vault TOKEN lifecycle: make a client token and get "
    "only its accessor (vault.token.create -- requires approval; MEHO "
    "never shows the new token), surgically revoke ONE token by its "
    "accessor (vault.token.revoke_accessor -- requires approval), "
    "list active token accessors (vault.token.list_accessors -- safe), "
    "and look up one token's TTL/policies/expiry by its accessor "
    "(vault.token.lookup_accessor -- safe). "
    "To make a token and store it straight in a Vault KV path or a "
    "Kubernetes Secret, so nobody sees it, use "
    "vault.token.create_and_store (requires approval). "
    "Token roles are saved sets of rules for making tokens (allowed "
    "policies, orphan, renewable, period): create or change one with "
    "vault.token.role.write (requires approval), read one with "
    "vault.token.role.read and list the names with vault.token.role.list "
    "(both safe). "
    "There is intentionally NO bulk-revoke op: never revoke broadly to "
    "recover from one leak. Accessors are non-secret reference handles. "
    "Route entity/group/policy questions to the 'identity' group."
)

#: Per-op registration spec for the token surface. ``group_key`` is
#: ``token`` for every row; ``safety_level`` / ``requires_approval`` are
#: carried per-op. Consumed by the package composer
#: ``ops_identity_token.register_vault_identity_token_operations``.
TOKEN_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": "vault.token.create",
        "handler": vault_token_create,
        "group_key": "token",
        "summary": "Make a Vault client token and return only its accessor.",
        "description": (
            "Makes a new Vault client token with explicit policies/TTL "
            "(POST /v1/auth/token/create). NON-IDEMPOTENT: each call makes "
            "a new token. MEHO never shows the new token: the result holds "
            "only the accessor and the policies, and the token is in no "
            "result, audit row, approval result or run record. The token is "
            "made under MEHO's own Vault login for this call, so Vault "
            "revokes it with that login unless it is an orphan. To get a "
            "token you can use, call vault.token.create_and_store, which "
            "makes an orphan token and writes it to a Vault KV path or a "
            "Kubernetes Secret. Classified credential_mint (aggregate-only "
            "broadcast). safety_level=dangerous, requires_approval=True. "
            "The accessor is a non-secret handle for "
            "vault.token.lookup_accessor and vault.token.revoke_accessor."
        ),
        "parameter_schema": VAULT_TOKEN_CREATE_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_CREATE_RESPONSE_SCHEMA,
        "tags": ["write", "credential-mint", "token", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_TOKEN_CREATE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.token.revoke_accessor",
        "handler": vault_token_revoke_accessor,
        "group_key": "token",
        "summary": "Revoke ONE token by its accessor (surgical; no bulk-revoke).",
        "description": (
            "Revokes exactly the one token an accessor references (POST "
            "/v1/auth/token/revoke-accessor). Surgical, single-accessor -- "
            "never a tree, never a bulk sweep. There is intentionally no "
            "bulk-revoke op (never revoke broadly to recover from one "
            "leak). safety_level=dangerous, requires_approval=True. The "
            "accessor is a reference handle, not the token secret."
        ),
        "parameter_schema": VAULT_TOKEN_REVOKE_ACCESSOR_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_REVOKE_ACCESSOR_RESPONSE_SCHEMA,
        "tags": ["write", "destructive", "token", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_TOKEN_REVOKE_ACCESSOR_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.token.list_accessors",
        "handler": vault_token_list_accessors,
        "group_key": "token",
        "summary": "List every active token's accessor handle.",
        "description": (
            "Enumerates every active token by its accessor handle (LIST "
            "/v1/auth/token/accessors). Accessors are non-secret "
            "references (never token secrets), usable to look up or "
            "surgically revoke one token. Read-only; registered "
            "safety_level=safe, requires_approval=False. An empty token "
            "store normalises to {'keys': []}."
        ),
        "parameter_schema": VAULT_TOKEN_LIST_ACCESSORS_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_LIST_ACCESSORS_RESPONSE_SCHEMA,
        "tags": ["read-only", "token", "identity"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_TOKEN_LIST_ACCESSORS_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.token.lookup_accessor",
        "handler": vault_token_lookup_accessor,
        "group_key": "token",
        "summary": "Look up one token's TTL/policies/expiry by its accessor.",
        "description": (
            "Reads one token's metadata by its accessor (POST "
            "/v1/auth/token/lookup-accessor) -- policies, ttl, expire_time, "
            "period (periodic tokens), display_name, creation_time, "
            "renewable. Answers 'what policies does this token carry and "
            "when does it expire' for a token audit / access review. "
            "Read-only; registered safety_level=safe, "
            "requires_approval=False. The accessor is a non-secret "
            "reference handle and Vault blanks the token id in the "
            "response, so no secret is exposed. Unlike list_accessors, an "
            "unknown accessor is a genuine not-found and surfaces as a "
            "connector_error (InvalidPath)."
        ),
        "parameter_schema": VAULT_TOKEN_LOOKUP_ACCESSOR_PARAMETER_SCHEMA,
        "response_schema": VAULT_TOKEN_LOOKUP_ACCESSOR_RESPONSE_SCHEMA,
        "tags": ["read-only", "token", "identity"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_TOKEN_LOOKUP_ACCESSOR_LLM_INSTRUCTIONS,
    },
)
