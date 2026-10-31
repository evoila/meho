# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schema pieces and op builders shared by the infrastructure op specs (:mod:`.infra_ops`)."""

from __future__ import annotations

from typing import Any, Final, Literal

from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, list_of, obj
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp

__all__ = [
    "COMMON_FIELDS",
    "FILTER",
    "INFRA_GROUP",
    "PAGING",
    "STATUS_NOTE",
    "TAG_FIELDS",
    "S",
    "change",
    "change_answer",
    "id_param",
    "list_answer",
    "read",
    "tags_param",
    "text_param",
]

INFRA_GROUP: Final[str] = "vcfa-tenant-infra"
S: Final = ["string", "null"]

# ---------------------------------------------------------------------------
# Schema fragments
# ---------------------------------------------------------------------------


def id_param(description: str) -> dict[str, Any]:
    """An id that goes into a URL path: not empty, not ``.`` or ``..``."""
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 256,
        "not": {"enum": [".", ".."]},
        "description": description,
    }


def text_param(description: str, *, max_length: int = 256) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length, "description": description}


_TAG_ITEM: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One tag.",
    "properties": {
        "key": text_param("The tag key."),
        "value": {
            "type": "string",
            "maxLength": 256,
            "description": "The tag value; may be empty.",
        },
    },
    "required": ["key"],
    "additionalProperties": False,
}


def tags_param(description: str) -> dict[str, Any]:
    return {"type": "array", "items": _TAG_ITEM, "description": description}


TAG_FIELDS: Final[dict[str, Any]] = {
    "key": field("string", "The tag key."),
    "value": field(S, "The tag value."),
}
PAGING: Final[dict[str, Any]] = {
    "$top": {"type": "integer", "minimum": 1, "description": "Optional page size; omit for all."},
    "$skip": {"type": "integer", "minimum": 0, "description": "Optional rows to skip."},
}
FILTER: Final[dict[str, Any]] = {
    "$filter": text_param(
        "Optional OData filter, for example \"name eq '<name>'\".", max_length=1024
    ),
}
COMMON_FIELDS: Final[dict[str, Any]] = {
    "id": field("string", "The id."),
    "name": field("string", "The name."),
    "description": field(S, "The description."),
    "externalRegionId": field(S, "The vCenter datacenter id of the region."),
    "cloudAccountId": field(S, "The cloud account the object belongs to."),
}
STATUS_NOTE: Final = (
    "'unchanged' means nothing was written (the object already looks like this); "
    "'invalid_request' names what to fix in guidance."
)


def list_answer(what: str, fields: dict[str, Any]) -> dict[str, Any]:
    return obj(
        f"The {what}.",
        {
            "items": list_of(f"One row per {what[:-1] if what.endswith('s') else what}.", fields),
            "totalElements": field("integer", f"How many {what} there are."),
        },
    )


def change_answer(
    key: str, what: str, fields: dict[str, Any], statuses: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "description": f"What the change did to the {what}.",
        "properties": {
            "status": field("string", "What happened.", enum=statuses),
            key: {**obj(f"The {what}.", fields), "type": ["object", "null"]},
            "changed": {
                "type": "array",
                "description": "The vendor fields that were sent.",
                "items": {"type": "string"},
            },
            "guidance": field(S, "What to do next, or why nothing was done."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    }


def read(
    prefix: str,
    *,
    name: str,
    path: str,
    summary: str,
    description: str,
    properties: dict[str, Any],
    example: dict[str, Any],
    response: dict[str, Any],
    when: str,
    shape: str,
    next_step: str,
    tag: str,
) -> VcfaTypedOp:
    return VcfaTypedOp(
        op_id=f"{prefix}.{name}",
        handler_attr=f"infra_{name.replace('.', '_')}",
        plane="tenant",
        path=path,
        summary=summary,
        description=description,
        parameter_schema={
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
            "examples": [example],
        },
        response_schema=response,
        group_key=INFRA_GROUP,
        tags=("read-only", "vcfa", tag),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=guidance(
            when_to_call=when,
            output_shape=shape + " A long list comes back as a link to the stored result; "
            "read it with result_query.",
            next_step=next_step,
        ),
    )


def change(
    prefix: str,
    *,
    name: str,
    path: str,
    summary: str,
    description: str,
    schema: dict[str, Any],
    response: dict[str, Any],
    safety: Literal["caution", "dangerous", "destructive"],
    when: str,
    preconditions: str,
    shape: str,
    next_step: str,
    tag: str,
    credential: bool = False,
) -> VcfaTypedOp:
    return VcfaTypedOp(
        op_id=f"{prefix}.{name}",
        handler_attr=f"infra_{name.replace('.', '_')}",
        plane="tenant",
        path=path,
        summary=summary,
        description=description,
        parameter_schema=schema,
        response_schema=response,
        group_key=INFRA_GROUP,
        tags=("write", "vcfa", tag, *(("credential",) if credential else ())),
        safety_level=safety,
        requires_approval=True,
        llm_instructions=guidance(
            when_to_call=when,
            preconditions=preconditions,
            output_shape=shape,
            next_step=next_step,
            result_scalars={"keys": ["status", "guidance"]},
        ),
    )
