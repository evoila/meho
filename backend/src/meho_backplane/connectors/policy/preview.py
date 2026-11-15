# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``policy.classify_preview`` operation: the dry run of a classification edit.

It takes the same parameters as ``policy.classify`` and lists every
operation the entry matches, with its category, labels, risk level and
"always wait" mark before and after the edit. It writes nothing, so it is
``safe`` and does not wait for approval. A tenant admin runs it before
``policy.classify``, above all for a pattern that can match many
operations. REST, the CLI and the console show the same dry run
(:mod:`meho_backplane.policy.bulk`).

Like ``policy.classify`` it exists only while the
``POLICY_CLASSIFICATION`` switch is on; its registrar deletes nothing
itself, because the ``policy.classify`` registrar removes every row of
the ``policy-registry`` connector while the switch is off.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.policy.classify import (
    GROUP_KEY,
    GROUP_WHEN_TO_USE,
    PARAMETER_SCHEMA,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.bulk import preview_classification_edit
from meho_backplane.policy.store import ClassificationEdit
from meho_backplane.settings import parse_bool_env

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "POLICY_CLASSIFY_PREVIEW_OP_ID",
    "policy_classify_preview",
    "register_policy_classify_preview_operation",
]

#: The op id of the dry run.
POLICY_CLASSIFY_PREVIEW_OP_ID: Final[str] = "policy.classify_preview"

_VALUES_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "category": {"type": ["string", "null"], "description": "null on a conflict."},
        "labels": {"type": "object", "additionalProperties": {"type": "string"}},
        "risk": {"type": ["string", "null"], "description": "null when no entry sets one."},
        "always_wait": {"type": "boolean"},
        "rule_ref": {"type": "string", "description": "The entry that decided the values."},
        "in_force": {
            "type": ["object", "null"],
            "description": (
                "What applies with these values: the risk level and whether a call waits, "
                "after the code's value, the overrides and the minimum-level rules. null "
                "when the operation is gone."
            ),
            "properties": {"risk": {"type": "string"}, "waits": {"type": "boolean"}},
            "required": ["risk", "waits"],
        },
    },
    "required": ["category", "labels", "risk", "always_wait", "rule_ref", "in_force"],
}

RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "match": {"type": "object", "description": "The match of the edit."},
        "rule_ref": {
            "type": "string",
            "description": "Where the entry would be, for example tenant:classifications[0].",
        },
        "entry": {"type": "object", "description": "The entry as it would be stored."},
        "matched_operations": {
            "type": "integer",
            "description": "How many operations your tenant can see that the entry matches.",
        },
        "changed_operations": {
            "type": "integer",
            "description": "How many of them the edit changes.",
        },
        "conflict": {
            "type": ["string", "null"],
            "description": "Why policy.classify would refuse the edit, or null.",
        },
        "operations": {
            "type": "array",
            "description": "Every matched operation, with its values before and after.",
            "items": {
                "type": "object",
                "properties": {
                    "connector_id": {"type": "string"},
                    "group_key": {"type": ["string", "null"]},
                    "op_id": {"type": "string"},
                    "before": _VALUES_SCHEMA,
                    "after": _VALUES_SCHEMA,
                    "changes": {"type": "boolean"},
                },
                "required": ["connector_id", "group_key", "op_id", "before", "after", "changes"],
            },
        },
    },
    "required": [
        "match",
        "rule_ref",
        "entry",
        "matched_operations",
        "changed_operations",
        "conflict",
        "operations",
    ],
}

_DESCRIPTION = (
    "Dry run of policy.classify: list every operation a classification entry would "
    "match, with its category, labels and always-wait mark before and after. Writes "
    "nothing."
)

_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Before policy.classify, to see which operations a connector, group or "
        "operation pattern matches and what changes for each."
    ),
    "preconditions": "Needs the tenant_admin role. The category must be in the taxonomy.",
    "output_shape": (
        "{'match', 'rule_ref', 'entry', 'matched_operations', 'changed_operations', "
        "'conflict', 'operations'}"
    ),
    "next_step": (
        "When conflict is null and the list is what you expect, call policy.classify "
        "with the same parameters. It waits until another tenant admin approves it."
    ),
}


async def policy_classify_preview(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Return the dry run of one classification edit of the operator's tenant.

    Op-id: ``policy.classify_preview``. Targetless and read-only.
    """
    edit = ClassificationEdit(
        match=params["match"],
        category=params.get("category"),
        labels=params.get("labels"),
        always_wait=params.get("always_wait"),
    )
    preview = await preview_classification_edit(operator, edit)
    return preview.to_dict()


async def register_policy_classify_preview_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``policy.classify_preview`` typed op while the switch is on."""
    if not parse_bool_env(os.environ.get("POLICY_CLASSIFICATION")):
        return
    await register_typed_operation(
        product="policy",
        version="1.x",
        impl_id="policy-registry",
        op_id=POLICY_CLASSIFY_PREVIEW_OP_ID,
        handler=policy_classify_preview,
        group_key=GROUP_KEY,
        when_to_use=GROUP_WHEN_TO_USE,
        summary="Dry run of a classification edit: the matched operations, before and after.",
        description=_DESCRIPTION,
        parameter_schema=PARAMETER_SCHEMA,
        response_schema=RESPONSE_SCHEMA,
        tags=["policy", "read", "rights"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
