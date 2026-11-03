# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registrar for the two handoff steps: ``secret.handoff.export`` / ``.import``.

Both ops live on the synthetic ``secret-broker-1.x`` connector next to
``secret.move``. Both are ``dangerous`` with ``requires_approval=True``,
so each one waits until a second person approves it on its own instance.
The handlers are in :mod:`.handoff`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.secret.handoff import (
    DEFAULT_HANDOFF_TTL_SECONDS,
    secret_handoff_export,
    secret_handoff_import,
)
from meho_backplane.connectors.secret.ops import SECRET_REF_PROPERTY
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "SECRET_HANDOFF_EXPORT_PARAMETER_SCHEMA",
    "SECRET_HANDOFF_IMPORT_PARAMETER_SCHEMA",
    "register_secret_handoff_operations",
]

#: Shortest and longest life a handoff may get. One minute is enough for a
#: scripted move; one day leaves room for the approval on the second instance.
_MIN_TTL_SECONDS: Final[int] = 60
_MAX_TTL_SECONDS: Final[int] = 86400

_SHA256_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "Hex SHA-256 of the value, never the value.",
}

_LENGTH_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "description": "Byte length of the value.",
}

_REASON_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Why the login is moved. Shown to the approver; must not contain a secret.",
}

SECRET_HANDOFF_EXPORT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "from": SECRET_REF_PROPERTY,
        "ttl_seconds": {
            "type": "integer",
            "minimum": _MIN_TTL_SECONDS,
            "maximum": _MAX_TTL_SECONDS,
            "default": DEFAULT_HANDOFF_TTL_SECONDS,
            "description": (
                "How long the handoff can be used, in seconds (60 to 86400, "
                "default 3600). Leave time for the approval on the other instance."
            ),
        },
        "reason": _REASON_PROPERTY,
    },
    "required": ["from"],
    "additionalProperties": False,
    "examples": [
        {
            "from": "vault:tenants/<tenant-id>/targets/db-prod#password",
            "ttl_seconds": 3600,
            "reason": "move target db-prod to the new MEHO instance",
        }
    ],
}

_EXPORT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "const": "exported"},
        "value_sha256": _SHA256_PROPERTY,
        "length": _LENGTH_PROPERTY,
        "handoff_token": {
            "type": "string",
            "description": (
                "One-time handoff for secret.handoff.import on the other instance. "
                "It is not the value; it works once and then expires."
            ),
        },
        "handoff_ttl_seconds": {
            "type": "integer",
            "description": "Seconds the handoff stays usable.",
        },
    },
    "required": ["status", "value_sha256", "length", "handoff_token", "handoff_ttl_seconds"],
    "additionalProperties": False,
}

SECRET_HANDOFF_IMPORT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handoff_token": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The handoff_token that secret.handoff.export returned on the "
                "other instance. Works once."
            ),
        },
        "store": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of a target on THIS instance (product 'vault') whose host is "
                "the other instance's Vault or OpenBao, where the handoff was made."
            ),
        },
        "to": SECRET_REF_PROPERTY,
        "expect_sha256": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{64}$",
            "description": (
                "The value_sha256 the export returned. When set, nothing is "
                "written if the value does not match."
            ),
        },
        "reason": _REASON_PROPERTY,
    },
    "required": ["handoff_token", "store", "to"],
    "additionalProperties": False,
    "examples": [
        {
            "handoff_token": "<handoff_token from secret.handoff.export>",
            "store": "old-instance-vault",
            "to": "vault:tenants/<tenant-id>/targets/db-prod#password",
            "expect_sha256": "0" * 64,
            "reason": "move target db-prod from the old MEHO instance",
        }
    ],
}

_IMPORT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "const": "imported"},
        "value_sha256": _SHA256_PROPERTY,
        "length": _LENGTH_PROPERTY,
    },
    "required": ["status", "value_sha256", "length"],
    "additionalProperties": False,
}

_EXPORT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Step 1 of moving a target's login to ANOTHER MEHO instance. Run it on "
        "the instance that has the login now. It puts the value into a one-time "
        "handoff in this instance's Vault. You never see the value."
    ),
    "preconditions": (
        "A second person must approve. The source ref must be inside your "
        "tenant. Run secret.handoff.import on the other instance before the "
        "handoff expires."
    ),
    "parameter_hints": {
        "from": "Required. Source '<kind>:<ref>', e.g. 'vault:<path>#<field>'.",
        "ttl_seconds": "Optional. Handoff life in seconds (60 to 86400, default 3600).",
        "reason": "Optional. Shown to the approver.",
    },
    "output_shape": (
        "{'status': 'exported', 'value_sha256', 'length', 'handoff_token', "
        "'handoff_ttl_seconds'}. The value never appears."
    ),
    "next_step": (
        "On the other instance call secret.handoff.import with this "
        "handoff_token, a 'store' target that points at this instance's Vault, "
        "the sink 'to', and expect_sha256 set to this value_sha256."
    ),
}

_IMPORT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Step 2 of moving a target's login from ANOTHER MEHO instance. Run it on "
        "the instance that gets the login. It uses the handoff once at the other "
        "instance's Vault and writes the value into this instance's store."
    ),
    "preconditions": (
        "A second person must approve. A target with product 'vault' must point "
        "at the other instance's Vault. The handoff must be unused and not expired."
    ),
    "parameter_hints": {
        "handoff_token": "Required. The handoff_token from secret.handoff.export.",
        "store": "Required. Name of the target here that points at the other Vault.",
        "to": "Required. Sink '<kind>:<ref>', e.g. 'vault:tenants/<tenant-id>/...#password'.",
        "expect_sha256": "Recommended. The value_sha256 from the export.",
        "reason": "Optional. Shown to the approver.",
    },
    "output_shape": ("{'status': 'imported', 'value_sha256', 'length'}. The value never appears."),
    "next_step": (
        "Compare value_sha256 with the export's value_sha256, then register or "
        "update the target here with its secret_ref pointing at the sink path."
    ),
}


async def register_secret_handoff_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert ``secret.handoff.export`` and ``secret.handoff.import``.

    Queued onto the lifespan registrar list by the package ``__init__``.
    Safe to run twice: unchanged text is not re-embedded.
    """
    common: dict[str, Any] = {
        "product": "secret",
        "version": "1.x",
        "impl_id": "secret-broker",
        "group_key": "broker",
        "safety_level": "dangerous",
        "requires_approval": True,
        "embedding_service": embedding_service,
    }
    await register_typed_operation(
        op_id="secret.handoff.export",
        handler=secret_handoff_export,
        when_to_use=_EXPORT_LLM_INSTRUCTIONS["when_to_call"],
        summary="Put a login into a one-time handoff for another MEHO instance.",
        description=(
            "Reads one secret field and puts it into a one-time Vault handoff "
            "(response wrapping) on this instance. Returns the handoff, the "
            "value's SHA-256 and its length, never the value. Needs approval."
        ),
        parameter_schema=SECRET_HANDOFF_EXPORT_PARAMETER_SCHEMA,
        response_schema=_EXPORT_RESPONSE_SCHEMA,
        tags=["secret-broker", "handoff", "change-class"],
        llm_instructions=_EXPORT_LLM_INSTRUCTIONS,
        **common,
    )
    await register_typed_operation(
        op_id="secret.handoff.import",
        handler=secret_handoff_import,
        when_to_use=_IMPORT_LLM_INSTRUCTIONS["when_to_call"],
        summary="Use a handoff from another MEHO instance once and store the login here.",
        description=(
            "Uses a one-time handoff at the other instance's Vault and writes "
            "the value into a sink on this instance. Returns only the status, "
            "the SHA-256 and the length, never the value. Needs approval."
        ),
        parameter_schema=SECRET_HANDOFF_IMPORT_PARAMETER_SCHEMA,
        response_schema=_IMPORT_RESPONSE_SCHEMA,
        tags=["secret-broker", "handoff", "write", "change-class"],
        llm_instructions=_IMPORT_LLM_INSTRUCTIONS,
        **common,
    )
