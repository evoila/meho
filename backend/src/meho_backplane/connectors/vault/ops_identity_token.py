# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Composer registrar for the Vault identity + token op groups (G3.15-T4).

The identity surface (entity / entity-alias / group lifecycle) lives in
:mod:`meho_backplane.connectors.vault.ops_identity`; the token surface
(create / revoke_accessor / list_accessors) lives in
:mod:`meho_backplane.connectors.vault.ops_token`. Each module owns its
handlers, schemas import, ``when_to_use`` blurb, and per-op spec table.

This thin module is the single lifespan-driven registrar entry (queued
from the package ``__init__`` alongside the KV / sys / auth registrars):
it walks both spec tables and upserts every row into
``endpoint_descriptor``. Writes register ``requires_approval=True``
(privilege assignment / irreversible removal / secret mint); the read
primitives register safe + approval-free so create-if-absent flows do
not stall on approval. ``vault.token.create``'s response token is
redacted at the classification layer (``credential_mint`` op-class), not
here.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vault.ops_identity import (
    IDENTITY_OP_SPECS,
    IDENTITY_WHEN_TO_USE,
    vault_identity_entity_alias_write,
    vault_identity_entity_read,
    vault_identity_entity_write,
    vault_identity_group_delete,
    vault_identity_group_read,
    vault_identity_group_write,
    vault_identity_list,
)
from meho_backplane.connectors.vault.ops_token import (
    TOKEN_OP_SPECS,
    TOKEN_WHEN_TO_USE,
    vault_token_create,
    vault_token_list_accessors,
    vault_token_revoke_accessor,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "register_vault_identity_token_operations",
    "vault_identity_entity_alias_write",
    "vault_identity_entity_read",
    "vault_identity_entity_write",
    "vault_identity_group_delete",
    "vault_identity_group_read",
    "vault_identity_group_write",
    "vault_identity_list",
    "vault_token_create",
    "vault_token_list_accessors",
    "vault_token_revoke_accessor",
]


async def register_vault_identity_token_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the Vault identity + token ops into ``endpoint_descriptor``.

    Walks :data:`~meho_backplane.connectors.vault.ops_identity.IDENTITY_OP_SPECS`
    and :data:`~meho_backplane.connectors.vault.ops_token.TOKEN_OP_SPECS`,
    selecting the group's ``when_to_use`` blurb per spec. Idempotent: a
    second call against unchanged descriptions is a no-op for the
    embedding pipeline via the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`.

    ``group_key`` (``identity`` / ``token``), ``safety_level``, and
    ``requires_approval`` are carried per-op in the spec rows.
    """
    specs: tuple[dict[str, Any], ...] = (*IDENTITY_OP_SPECS, *TOKEN_OP_SPECS)
    for spec in specs:
        when_to_use = TOKEN_WHEN_TO_USE if spec["group_key"] == "token" else IDENTITY_WHEN_TO_USE
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
            group_key=spec["group_key"],
            when_to_use=when_to_use,
            tags=spec["tags"],
            safety_level=spec["safety_level"],
            requires_approval=spec["requires_approval"],
            llm_instructions=spec["llm_instructions"],
            embedding_service=embedding_service,
        )
