# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op-metadata row for ``keycloak.client_scope.protocol_mapper.update``.

The handler is in
:mod:`~meho_backplane.connectors.keycloak.ops_write_scope_mapper`.
:mod:`~meho_backplane.connectors.keycloak.ops_write_schemas` folds
:data:`SCOPE_MAPPER_OPS` into the connector's ``WRITE_OPS``. The row is
``dangerous`` with ``requires_approval=True``: a mapper decides what goes
into the login token, so only a MEHO-SA-Operator or a tenant admin may
approve, and the self-approval switch does not cover it.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp

__all__ = ["SCOPE_MAPPER_OPS"]

_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope_id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "Client scope internal UUID (from keycloak.client_scope.list).",
        },
        "scope_name": {
            "type": "string",
            "description": "Exact client scope name (used when scope_id is absent).",
        },
        "mapper_id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "Protocol mapper UUID (the id in the scope's protocolMappers list).",
        },
        "mapper_name": {
            "type": "string",
            "description": "Exact mapper name on that scope (used when mapper_id is absent).",
        },
        "representation": {
            "type": "object",
            "description": (
                "Only the mapper fields to change. A field not sent stays as it is. "
                "``config`` merges key by key. The mapper ``id`` cannot be changed."
            ),
            "minProperties": 1,
            "additionalProperties": True,
        },
    },
    "required": ["representation"],
    "allOf": [
        {"anyOf": [{"required": ["scope_id"]}, {"required": ["scope_name"]}]},
        {"anyOf": [{"required": ["mapper_id"]}, {"required": ["mapper_name"]}]},
    ],
    "additionalProperties": False,
    "examples": [
        {
            "scope_name": "<client-scope-name>",
            "mapper_name": "<mapper-name>",
            "representation": {"config": {"claim.value": "<new-claim-value>"}},
        }
    ],
}

_RESULT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope_id": {"type": "string", "description": "The client scope UUID."},
        "scope_name": {
            "type": ["string", "null"],
            "description": "The scope name from the request; null when only scope_id was passed.",
        },
        "mapper_id": {"type": "string", "description": "The protocol mapper UUID."},
        "mapper_name": {
            "type": ["string", "null"],
            "description": "The mapper name from the request; null when only mapper_id was passed.",
        },
        "changed_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of the fields the caller sent. Never values.",
        },
        "unrequested_changes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of fields that changed without being asked. Never values.",
        },
        "read_back_checked": {
            "type": "boolean",
            "description": "False when the read after the write failed. The write itself ran.",
        },
    },
    "additionalProperties": True,
}

SCOPE_MAPPER_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.client_scope.protocol_mapper.update",
        handler_attr="client_scope_protocol_mapper_update",
        summary="Change one protocol mapper of a Keycloak client scope (approval-gated).",
        description=(
            "Finds a client scope and one of its protocol mappers by id or exact "
            "name, then changes only the fields in ``representation``. Other "
            "fields stay. ``config`` merges key by key. Safe to run twice. "
            "requires_approval=True."
        ),
        parameter_schema=_PARAMS,
        response_schema=_RESULT,
        group_key="protocol_mapper_write",
        tags=("write", "protocol-mapper", "client-scope", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Use to change a protocol mapper that already exists on a client "
                "scope (``keycloak.client_scope.protocol_mapper.update``). Example: "
                "add one value to a hardcoded-claim mapper, so more people get a "
                "claim in their token. A second hardcoded-claim mapper with the "
                "same claim name does not add values; it overwrites the first. "
                "Change the existing mapper instead. A second person must approve. "
                "To add a mapper to a client, use ``keycloak.protocol_mapper.create``."
            ),
            "when_to_call": "When the claim a client scope puts into tokens must change.",
            "preconditions": (
                "The client scope and the mapper exist in the target's managed realm. "
                "Read them first with keycloak.client_scope.list."
            ),
            "next_step": (
                "Run keycloak.client_scope.list and check the new value in the mapper's config."
            ),
            "parameter_hints": {
                "scope_id": "Client scope UUID (from keycloak.client_scope.list).",
                "scope_name": "Exact scope name; resolved to UUID when scope_id is absent.",
                "mapper_id": "Mapper UUID from the scope's protocolMappers list.",
                "mapper_name": "Exact mapper name; resolved to UUID when mapper_id is absent.",
                "representation": (
                    "Only the fields to change, for example {'config': {'claim.value': '<value>'}}."
                ),
            },
            "output_shape": (
                "``{scope_id, scope_name, mapper_id, mapper_name, changed_fields, "
                "unrequested_changes, read_back_checked}``. Names only, no values."
            ),
        },
    ),
)
