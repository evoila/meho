# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault *token* op handlers + spec table (G3.15-T4, #1412).

The token group surfaces the core Vault ``token`` auth backend (always
mounted):

* ``vault.token.create`` mints a client token (``requires_approval=True``;
  the token is in the **response** -> ``credential_mint`` redaction).
* ``vault.token.revoke_accessor`` surgically revokes a single token by
  accessor (``requires_approval=True``).
* ``vault.token.list_accessors`` lists accessor handles
  (``safety_level="safe"``).

There is **no bulk-revoke op** by design -- never revoke broadly to
recover from one leak (the vault skill's loudest Don't-rule).

**Secret redaction is at the classification layer, not the handler**
(#1397 / #1401). ``vault.token.create``'s minted client token rides in
the *response* (under Vault's ``auth`` envelope);
:func:`meho_backplane.broadcast.events.classify_op` maps it to
``credential_mint`` (allowlist consulted BEFORE the ``.create``
write-suffix), so the broadcast collapses to aggregate-only and the
audit row holds only a ``params_hash``. The caller's ``OperationResult``
still carries the token (the whole point of minting). A token
**accessor** (``revoke_accessor`` param, ``list_accessors`` response) is
a reference handle, not the token secret -- deliberately not redacted.

Handler shape mirrors :mod:`meho_backplane.connectors.vault.ops_auth`:
``async def (operator, target, params) -> dict``, raises on failure,
forwards the operator JWT via ``vault_client_for_operator``, offloads the
blocking hvac call with ``asyncio.to_thread``. The ``token`` backend is
core (always mounted); ``list_accessors`` normalises an empty-store
``404`` to ``{"keys": []}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops_auth import _extract_keys
from meho_backplane.connectors.vault.ops_token_schemas import (
    VAULT_TOKEN_CREATE_LLM_INSTRUCTIONS,
    VAULT_TOKEN_CREATE_PARAMETER_SCHEMA,
    VAULT_TOKEN_CREATE_RESPONSE_SCHEMA,
    VAULT_TOKEN_LIST_ACCESSORS_LLM_INSTRUCTIONS,
    VAULT_TOKEN_LIST_ACCESSORS_PARAMETER_SCHEMA,
    VAULT_TOKEN_LIST_ACCESSORS_RESPONSE_SCHEMA,
    VAULT_TOKEN_REVOKE_ACCESSOR_LLM_INSTRUCTIONS,
    VAULT_TOKEN_REVOKE_ACCESSOR_PARAMETER_SCHEMA,
    VAULT_TOKEN_REVOKE_ACCESSOR_RESPONSE_SCHEMA,
)

__all__ = [
    "TOKEN_OP_SPECS",
    "TOKEN_WHEN_TO_USE",
    "vault_token_create",
    "vault_token_list_accessors",
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
    """Mint a fresh Vault client token (NON-IDEMPOTENT; token in RESPONSE).

    Op-id: ``vault.token.create``. Delegates to hvac's
    ``client.auth.token.create(policies=..., ttl=..., ...)``. Each call
    mints a brand-new token -- the op is non-idempotent.

    The minted ``client_token`` lands in the *response* (under Vault's
    ``auth`` envelope), so the op classifies ``credential_mint``: the
    broadcast collapses to aggregate-only and the audit row holds only a
    ``params_hash``, so the token never reaches the feed or the audit
    store. The caller's ``OperationResult`` still carries it (the whole
    point of minting) -- store it immediately. The ``accessor`` returned
    alongside is a non-secret reference handle (usable for
    ``vault.token.revoke_accessor``).
    """
    kwargs: dict[str, Any] = {}
    for field in _TOKEN_CREATE_FIELDS:
        if field in params and params[field] is not None:
            kwargs[field] = params[field]
    # ``ttl_period`` maps onto hvac's ``period=`` (periodic-token knob);
    # renamed in the schema to avoid clashing with the ``ttl`` field.
    if params.get("ttl_period") is not None:
        kwargs["period"] = params["ttl_period"]

    async with _auth_vault.vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(client.auth.token.create, **kwargs)

    auth = dict(payload["auth"])
    result: dict[str, Any] = {
        "client_token": auth["client_token"],
        "accessor": auth["accessor"],
    }
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

    async with _auth_vault.vault_client_for_operator(operator) as client:
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
    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            payload = await asyncio.to_thread(client.auth.token.list_accessors)
        except _hvac_invalid_path():
            return {"keys": []}

    return {"keys": _extract_keys(payload)}


# --- spec table ------------------------------------------------------------

TOKEN_WHEN_TO_USE: str = (
    "Use to manage Vault TOKEN lifecycle: mint a client token "
    "(vault.token.create -- requires approval; the token is secret and "
    "redacted from audit/broadcast), surgically revoke ONE token by its "
    "accessor (vault.token.revoke_accessor -- requires approval), and "
    "list active token accessors (vault.token.list_accessors -- safe). "
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
        "summary": "Mint a fresh Vault client token (non-idempotent; token in response).",
        "description": (
            "Mints a brand-new Vault client token with explicit "
            "policies/TTL (POST /v1/auth/token/create). NON-IDEMPOTENT: "
            "each call yields a distinct token. The client_token lands in "
            "the response, so the op is classified credential_mint: the "
            "broadcast collapses to aggregate-only and the audit row holds "
            "only a params_hash, so the minted token never reaches the "
            "feed or audit store. The caller's OperationResult still "
            "carries it -- store it immediately. safety_level=dangerous, "
            "requires_approval=True. The accessor returned alongside is a "
            "non-secret handle for vault.token.revoke_accessor."
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
)
