# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``policy.classify`` operation: change one classification entry of a tenant.

A classification entry gives connectors, operation groups or operations
(matched by pattern) a category, labels or the "always wait" mark. A
category decides who may approve, so every such change is a change of
rights:

* the operation is ``dangerous`` and always waits for approval;
* every request carries the rights-change mark
  (:func:`~meho_backplane.operations.grant_approval.register_rights_change_op`),
  so the person who asked can never approve it, and the approver must be
  another admin of the tenant;
* a person needs the ``tenant_admin`` role to ask
  (:func:`~meho_backplane.operations.role_floors.register_role_floor`).

The change runs only after that approval. It is checked again when it
runs (:func:`~meho_backplane.policy.store.set_classification`): an unknown
category, or two entries of the same level that would give one operation
different categories, refuse it and nothing is written.

The operation exists only while the ``POLICY_CLASSIFICATION`` switch is
on: the registrar does nothing when it is off, and the handler refuses a
call when the switch was turned off after the start.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.store import (
    ClassificationEdit,
    classification_enabled,
    set_classification,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "POLICY_CLASSIFY_OP_ID",
    "POLICY_REGISTRY_CONNECTOR_ID",
    "policy_classify",
    "register_policy_classify_operation",
]

#: Wire connector id of MEHO's own policy operations.
POLICY_REGISTRY_CONNECTOR_ID: Final[str] = "policy-registry-1.x"

#: The op id of a classification change.
POLICY_CLASSIFY_OP_ID: Final[str] = "policy.classify"

#: What a call says while the switch is off.
SWITCH_OFF_REFUSAL: Final[str] = (
    "Classification is off on this MEHO. An admin turns it on with the "
    "POLICY_CLASSIFICATION setting."
)

_PATTERN_HINT = "`*` is any text, `?` is one character, no wildcard is an exact match."

PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["match"],
    "properties": {
        "match": {
            "type": "object",
            "description": (
                "What the entry classifies. Give at least one field; every field you "
                "give must match. An operation entry beats a group entry, which beats a "
                "connector entry."
            ),
            "additionalProperties": False,
            "minProperties": 1,
            "properties": {
                "connector": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": f"Connector id pattern, for example vault-1.x. {_PATTERN_HINT}",
                },
                "group": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": f"Operation group key pattern. {_PATTERN_HINT}",
                },
                "op": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": f"Operation id pattern. {_PATTERN_HINT}",
                },
            },
        },
        "category": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_-]{0,62}$",
            "description": "A category of the taxonomy, for example network or storage.",
        },
        "labels": {
            "type": "object",
            "maxProperties": 32,
            "description": (
                "Free key=value text. Replaces the labels of this entry. Kept apart from "
                "the operation's tags."
            ),
            "additionalProperties": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "The value of one label.",
            },
        },
        "always_wait": {
            "type": "boolean",
            "description": (
                "true: every call of a matched operation waits for approval. false: back "
                "to the operation's own value, never below it."
            ),
        },
    },
    "examples": [
        {
            "match": {"connector": "<connector-id>", "group": "<group-key>"},
            "category": "network",
            "labels": {"team": "<team-name>"},
        },
        {"match": {"op": "<op-id-pattern>"}, "always_wait": True},
    ],
}

RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "scope": {"type": "string", "description": "Always tenant: the change is for your tenant."},
        "rule_ref": {
            "type": "string",
            "description": "Where the entry is now, for example tenant:classifications[0].",
        },
        "entry": {"type": "object", "description": "The stored entry after the change."},
        "matched_operations": {
            "type": "integer",
            "description": "How many operations your tenant can see that the entry matches.",
        },
    },
    "required": ["scope", "rule_ref", "entry", "matched_operations"],
}

_GROUP_KEY = "policy"
_GROUP_WHEN_TO_USE = (
    "Change how MEHO classifies operations for your tenant: the category, free "
    "labels and the always-wait mark of connectors, operation groups and operations. "
    "A category decides who may approve, so every change here is a change of rights "
    "and waits until a second tenant admin approves it."
)

_DESCRIPTION = (
    "Set the category, labels or always-wait mark of one classification entry of "
    "your tenant, matched by connector, group or operation pattern. A change of "
    "rights: it waits until another tenant admin approves it."
)

_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "A tenant admin wants connectors, groups or operations in a category (for "
        "example network), wants labels on them, or wants them to always wait."
    ),
    "preconditions": (
        "Needs the tenant_admin role. The call always waits for approval, and the "
        "person who asked can never approve it: another tenant admin must. The "
        "category must be in the taxonomy."
    ),
    "output_shape": "{'scope', 'rule_ref', 'entry', 'matched_operations'}",
    "next_step": (
        "Ask another tenant admin to approve the request. After the approval, "
        "search_operations and the call contract show the new values."
    ),
}


async def policy_classify(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Store one classification entry of the operator's tenant.

    Op-id: ``policy.classify``. Targetless. Runs after the approval; the
    tenant comes from *operator*, never from *params*.
    """
    if not classification_enabled():
        raise ValueError(SWITCH_OFF_REFUSAL)
    edit = ClassificationEdit(
        match=params["match"],
        category=params.get("category"),
        labels=params.get("labels"),
        always_wait=params.get("always_wait"),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        result = await set_classification(
            session, tenant_id=operator.tenant_id, set_by_sub=operator.sub, edit=edit
        )
    return {"scope": "tenant", **result}


async def register_policy_classify_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``policy.classify`` typed op; nothing while the switch is off."""
    if not classification_enabled():
        return
    await register_typed_operation(
        product="policy",
        version="1.x",
        impl_id="policy-registry",
        op_id=POLICY_CLASSIFY_OP_ID,
        handler=policy_classify,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="Set the category, labels or always-wait mark of a classification entry.",
        description=_DESCRIPTION,
        parameter_schema=PARAMETER_SCHEMA,
        response_schema=RESPONSE_SCHEMA,
        tags=["policy", "write", "rights"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
