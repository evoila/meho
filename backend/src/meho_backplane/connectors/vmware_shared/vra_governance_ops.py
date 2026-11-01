# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the approval and policy operations tell the agent, and the shared registrar.

:func:`governance_op_specs` builds the metadata of the four operations for
one id prefix (``vcfa.vra8`` or ``vcfa.tenant``). The ABX and subscription
specs are in :mod:`.vra_extensibility_ops`. :func:`register_vra_shared_ops`
registers a connector's specs as typed operations. The handlers are in
:mod:`.vra_governance` and :mod:`.vra_extensibility`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from meho_backplane.connectors.vmware_shared.vra_governance import (
    APPROVAL_ACTION_PATH,
    APPROVALS_PATH,
    POLICIES_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "APPROVAL_STATES",
    "VraOpSpec",
    "f",
    "governance_op_specs",
    "governance_when_to_use",
    "id_param",
    "obj",
    "register_vra_shared_ops",
    "rows",
]

#: The approval states the vendor documents (``DeploymentApprovalRequest.status``).
APPROVAL_STATES: Final[tuple[str, ...]] = (
    "PENDING",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "CANCELLED",
)

_S: Final[list[str]] = ["string", "null"]


@dataclass(frozen=True)
class VraOpSpec:
    """One operation, without the connector's group key and plane.

    ``group`` names the shared group (``governance`` or ``extensibility``);
    each connector maps it to its own group key.
    """

    op_id: str
    handler_attr: str
    path: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    tags: tuple[str, ...]
    safety_level: Literal["safe", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any]
    group: Literal["governance", "extensibility"]


def f(type_: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    """One schema field with its description."""
    return {"type": type_, "description": description, **extra}


def obj(description: str, properties: dict[str, Any], *, nullable: bool = False) -> dict[str, Any]:
    """An object schema that names its fields."""
    return {
        "type": ["object", "null"] if nullable else "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def rows(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """A flat list of rows."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": properties, "additionalProperties": True},
    }


def id_param(description: str) -> dict[str, Any]:
    """A required-looking id string: never empty."""
    return {"type": "string", "minLength": 1, "maxLength": 255, "description": description}


def _paging(page_text: str) -> dict[str, Any]:
    return {
        "page": {"type": "integer", "minimum": 0, "description": page_text},
        "size": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "description": "Optional page size.",
        },
    }


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def _approval_fields() -> dict[str, Any]:
    return {
        "id": f(_S, "The approval id; give it to the decide op."),
        "status": f(_S, "The approval state.", enum=[*APPROVAL_STATES, None]),
        "action": f(_S, "What waits, for example Create or a day-2 action."),
        "requestId": f(_S, "The Automation request that waits."),
        "requestedBy": f(_S, "Who asked."),
        "deploymentId": f(_S, "The deployment the request is for."),
        "deploymentName": f(_S, "The deployment name."),
        "projectId": f(_S, "The project id."),
        "projectName": f(_S, "The project name."),
        "catalogItemName": f(_S, "The catalog item, when the request came from the catalog."),
        "templateName": f(_S, "The cloud template, when there is one."),
        "policyNames": {
            "type": "array",
            "description": "The approval policies that apply.",
            "items": {"type": "string", "description": "A policy name."},
        },
        "pendingApprovers": {
            "type": "array",
            "description": "The approvers who have not decided yet.",
            "items": {"type": "string", "description": "A user or group."},
        },
        "currentPhase": f(["integer", "null"], "The approval phase now running."),
        "createdAt": f(_S, "When the approval was made."),
        "expiryAt": f(_S, "When it expires."),
    }


def _approval_list_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.approval.list",
        handler_attr="vra_approval_list",
        path=APPROVALS_PATH,
        summary="List requests that wait for an approval in Automation, or were decided.",
        description=(
            "Reads GET /approval/api/approvals as flat rows: what waits, who asked, the "
            "deployment, the project and the approvers still to decide. Give state PENDING "
            "for the waiting ones. Paged with page and size."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": list(APPROVAL_STATES),
                    "description": "Optional state filter; PENDING lists what waits.",
                },
                "search": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional text to find in deployment, project, user or action.",
                },
                **_paging("Optional page number, starting at 0."),
            },
            "additionalProperties": False,
            "examples": [{"state": "PENDING"}],
        },
        response_schema=obj(
            "The approvals.",
            {
                "approvals": rows("One row per approval.", _approval_fields()),
                "totalElements": f(["integer", "null"], "How many match, on all pages."),
            },
        ),
        tags=("read-only", "vcfa", "approval"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": "Call to see which Automation requests wait for an approval.",
            "output_shape": (
                "{approvals: [{id, status, action, requestId, requestedBy, deploymentName, "
                "projectName, catalogItemName, policyNames, pendingApprovers, createdAt, "
                "expiryAt}], totalElements}. A long list comes back as a link to the stored "
                "result; read it with result_query."
            ),
            "next_step": f"Approve or reject one with {prefix}.approval.decide and its id.",
        },
        group="governance",
    )


def _approval_decide_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.approval.decide",
        handler_attr="vra_approval_decide",
        path=APPROVAL_ACTION_PATH,
        summary="Approve or reject a request that waits for an approval in Automation.",
        description=(
            "Sends POST /approval/api/approvals/action with APPROVE or REJECT. MEHO first "
            "waits until a person approves this call. When the approval no longer waits, "
            "nothing is sent."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": id_param(f"The approval id (from {prefix}.approval.list)."),
                "decision": {
                    "type": "string",
                    "enum": ["APPROVE", "REJECT"],
                    "description": "APPROVE lets the request run; REJECT stops it.",
                },
                "comment": {
                    "type": "string",
                    "maxLength": 1024,
                    "description": "Optional comment, kept with the decision in Automation.",
                },
            },
            "required": ["id", "decision"],
            "additionalProperties": False,
            "examples": [{"id": "<approval-id>", "decision": "APPROVE", "comment": "<comment>"}],
        },
        response_schema=obj(
            "What the op did.",
            {
                "status": f("string", "decided or not_pending.", enum=["decided", "not_pending"]),
                "decision": f(_S, "The decision sent; null when nothing was sent."),
                "approval": obj("The approval after the call.", _approval_fields()),
                "guidance": f(_S, "A short note on what to do next."),
            },
        ),
        tags=("write", "vcfa", "approval"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_call": "Call to approve or reject one Automation request that waits.",
            "preconditions": (
                f"Find the approval with {prefix}.approval.list (state PENDING). A second "
                "person must approve this call in MEHO first; an agent needs a grant. The "
                "Automation login of the target must be an approver of the request."
            ),
            "output_shape": (
                "{status: decided | not_pending, decision, approval: {id, status, requestId, "
                "deploymentName, pendingApprovers, ...}, guidance}."
            ),
            "next_step": (
                f"Read {prefix}.approval.list to see if more approvers must decide, and "
                f"{prefix}.request.get with approval.requestId to follow the request."
            ),
        },
        group="governance",
    )


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

_POLICY_TYPES_TEXT: Final[str] = (
    "The policy type, for example com.vmware.policy.approval, "
    "com.vmware.policy.deployment.lease, com.vmware.policy.deployment.action (day-2 "
    "actions) or com.vmware.policy.catalog.entitlement (content sharing)."
)


def _policy_fields() -> dict[str, Any]:
    return {
        "id": f(_S, "The policy id."),
        "name": f(_S, "The policy name."),
        "description": f(_S, "The policy description."),
        "typeId": f(_S, _POLICY_TYPES_TEXT),
        "enforcementType": f(_S, "HARD or SOFT.", enum=["HARD", "SOFT", None]),
        "projectId": f(_S, "The project, for a policy of one project."),
        "orgId": f(_S, "The org of the policy."),
        "createdAt": f(_S, "When the policy was made."),
        "createdBy": f(_S, "Who made it."),
        "lastUpdatedAt": f(_S, "When it last changed."),
        "lastUpdatedBy": f(_S, "Who changed it last."),
        "definition": f(["object", "null"], "The settings of the policy type (secrets hidden)."),
        "criteria": f(["object", "null"], "Which objects the policy applies to."),
        "scopeCriteria": f(["object", "null"], "Which projects the policy applies to."),
    }


def _policy_list_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.policy.list",
        handler_attr="vra_policy_list",
        path=POLICIES_PATH,
        summary="List policies: approval, lease, day-2 actions, content sharing and more.",
        description=(
            "Reads GET /policy/api/policies as flat rows: name, type, HARD or SOFT, project "
            "and who changed it. Give with_definition for the settings of each policy. Paged "
            "with $top and $skip."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional text at the start of the name or in the description.",
                },
                "with_definition": {
                    "type": "boolean",
                    "description": "Optional; true returns the settings (definition) too.",
                },
                "$top": {"type": "integer", "minimum": 1, "description": "Optional page size."},
                "$skip": {"type": "integer", "minimum": 0, "description": "Optional rows to skip."},
            },
            "additionalProperties": False,
            "examples": [{"search": "<policy-name>", "with_definition": True}],
        },
        response_schema=obj(
            "The policies.",
            {
                "policies": rows("One row per policy.", _policy_fields()),
                "totalElements": f(["integer", "null"], "How many match, on all pages."),
            },
        ),
        tags=("read-only", "vcfa", "policy"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to see which approval, lease, day-2 or sharing policies apply, or "
                "before you change one."
            ),
            "output_shape": (
                "{policies: [{id, name, description, typeId, enforcementType, projectId, "
                "lastUpdatedAt, definition, criteria, scopeCriteria}], totalElements}. A long "
                "list comes back as a link to the stored result; read it with result_query."
            ),
            "next_step": f"Create or change a policy with {prefix}.policy.set.",
        },
        group="governance",
    )


def _policy_set_params(prefix: str) -> dict[str, Any]:
    any_object = {"type": "object", "additionalProperties": True}
    return {
        "type": "object",
        "properties": {
            "id": id_param(
                f"Optional id of the policy to change (from {prefix}.policy.list). Without "
                "it, the policy with the same name, type and project is changed, or a new "
                "one is made."
            ),
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "The policy name.",
            },
            "type_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": _POLICY_TYPES_TEXT,
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional policy description.",
            },
            "enforcement_type": {
                "type": "string",
                "enum": ["HARD", "SOFT"],
                "description": "Optional; HARD (the default) wins over SOFT policies.",
            },
            "project_id": id_param(
                "Optional project, for a policy of one project. Not with scope_criteria."
            ),
            "definition": {
                **any_object,
                "description": (
                    "The settings of the policy type, as the vendor names them, for example "
                    "the lease days of a lease policy."
                ),
            },
            "criteria": {
                **any_object,
                "description": "Optional filter of the objects the policy applies to.",
            },
            "scope_criteria": {
                **any_object,
                "description": "Optional filter of the projects it covers. Not with project_id.",
            },
        },
        "required": ["name", "type_id", "definition"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<policy-name>",
                "type_id": "com.vmware.policy.deployment.lease",
                "enforcement_type": "HARD",
                "project_id": "<project-id>",
                "definition": {"leaseGrace": 1, "leaseTermMax": 30, "leaseTotalTermMax": 90},
            }
        ],
    }


def _policy_set_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.policy.set",
        handler_attr="vra_policy_set",
        path=POLICIES_PATH,
        summary="Create or change a policy, after a dry run of the vendor.",
        description=(
            "Sends POST /policy/api/policies?dryRun=true, then with dryRun=false. Without id, "
            "the policy with the same name, type and project is changed, so it is safe to run "
            "twice. When nothing differs, nothing is sent."
        ),
        parameter_schema=_policy_set_params(prefix),
        response_schema=obj(
            "What the op did.",
            {
                "status": f(
                    "string",
                    "created, changed, unchanged or invalid_request.",
                    enum=["created", "changed", "unchanged", "invalid_request"],
                ),
                "policy": obj("The policy after the call.", _policy_fields(), nullable=True),
                "guidance": f(_S, "A short note on what to do next."),
            },
        ),
        tags=("write", "vcfa", "policy"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call to make or change an approval, lease, day-2 action or sharing policy."
            ),
            "preconditions": (
                f"Read the current policies with {prefix}.policy.list (with_definition true) "
                "and copy the definition shape of the same type. A second person must "
                "approve the call; an agent needs a grant."
            ),
            "output_shape": (
                "{status: created | changed | unchanged | invalid_request, policy: {id, name, "
                "typeId, enforcementType, projectId, definition, ...}, guidance}."
            ),
            "next_step": (
                f"Check the policy with {prefix}.policy.list. A vendor error on the dry run "
                "means nothing was saved; fix the definition and call again."
            ),
        },
        group="governance",
    )


def governance_op_specs(prefix: str) -> tuple[VraOpSpec, ...]:
    """The four approval and policy operations for the op id *prefix*."""
    return (
        _approval_list_spec(prefix),
        _approval_decide_spec(prefix),
        _policy_list_spec(prefix),
        _policy_set_spec(prefix),
    )


def governance_when_to_use(prefix: str, product: str) -> str:
    """The group text: which reads and which changes the group holds."""
    return (
        f"Approvals and policies of {product}. Reads: list the requests that wait for an "
        f"approval or were decided ({prefix}.approval.list) and list the approval, lease, "
        f"day-2 action and sharing policies ({prefix}.policy.list). Changes: approve or "
        f"reject a waiting request ({prefix}.approval.decide) and create or change a policy "
        f"({prefix}.policy.set); a second person must approve each change in MEHO first."
    )


async def register_vra_shared_ops(
    cls: Any,
    specs: Sequence[VraOpSpec],
    *,
    group_keys: Mapping[str, str],
    group_texts: Mapping[str, str],
    extra_tags: tuple[str, ...],
    embedding_service: EmbeddingService | None = None,
) -> int:
    """Register *specs* as typed ops of the connector class *cls*; return the count.

    ``group_keys`` maps a spec's ``group`` to the connector's group key, and
    ``group_texts`` maps that key to its ``when_to_use`` text. Each spec's
    ``handler_attr`` must be a method of *cls*.
    """
    # Lazy import: the operations package pulls in the embedding pipeline.
    from meho_backplane.operations.typed_register import register_typed_operation

    for spec in specs:
        handler = getattr(cls, spec.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"{cls.__name__} has no handler {spec.handler_attr!r} for {spec.op_id!r}"
            )
        group_key = group_keys[spec.group]
        await register_typed_operation(
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
            op_id=spec.op_id,
            handler=handler,
            summary=spec.summary,
            description=spec.description,
            parameter_schema=spec.parameter_schema,
            response_schema=spec.response_schema,
            group_key=group_key,
            when_to_use=group_texts[group_key],
            tags=[*spec.tags, *extra_tags],
            safety_level=spec.safety_level,
            requires_approval=spec.requires_approval,
            llm_instructions=spec.llm_instructions,
            embedding_service=embedding_service,
        )
    return len(specs)
