# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Register the five ``spaces.*`` typed operations.

Natural key ``(product="spaces", version="1.x", impl_id="spaces-records")``,
so the wire connector id is ``spaces-records-1.x``.

Risk levels: the four writes are ``caution`` and need approval; the overview
is ``safe``. The four writes need the ``tenant_admin`` role from a person
(role floor, checked on every path) and the handler checks it again for the
login that runs the call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.spaces import schemas as s
from meho_backplane.connectors.spaces.overview import spaces_overview
from meho_backplane.connectors.spaces.writes import (
    spaces_assign,
    spaces_member_disable,
    spaces_reassign,
    spaces_unassign,
)
from meho_backplane.operations.role_floors import register_role_floor
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["register_spaces_operations"]

_Spec = tuple[
    str,  # op id
    Callable[..., Any],  # handler
    str,  # summary
    str,  # description
    dict[str, Any],  # parameters
    dict[str, Any],  # response
    dict[str, Any],  # guidance
    list[str],  # tags
    Literal["safe", "caution"],  # safety level
]

_WRITE_TAGS = ["spaces", "write", "ownership"]

_SPECS: tuple[_Spec, ...] = (
    (
        "spaces.assign",
        spaces_assign,
        "Assign a target, machine, service or fact to a space.",
        "Gives one system to a space of your tenant, with a reason. Needs the "
        "tenant_admin role. The same system twice in one space changes nothing.",
        s.ASSIGN_PARAMETERS,
        s.ASSIGN_RESPONSE,
        s.ASSIGN_GUIDANCE,
        _WRITE_TAGS,
        "caution",
    ),
    (
        "spaces.unassign",
        spaces_unassign,
        "End the assignment of a system to a space.",
        "Ends one current assignment with a reason. The row stays, with an end "
        "time. Needs the tenant_admin role.",
        s.UNASSIGN_PARAMETERS,
        s.UNASSIGN_RESPONSE,
        s.UNASSIGN_GUIDANCE,
        _WRITE_TAGS,
        "caution",
    ),
    (
        "spaces.reassign",
        spaces_reassign,
        "Move an assignment to another space in one step.",
        "Ends the current assignment and starts a new one in another space of "
        "your tenant, with a reason. Needs the tenant_admin role.",
        s.REASSIGN_PARAMETERS,
        s.REASSIGN_RESPONSE,
        s.REASSIGN_GUIDANCE,
        _WRITE_TAGS,
        "caution",
    ),
    (
        "spaces.member.disable",
        spaces_member_disable,
        "End one person's membership in one space (leave a lab).",
        "Ends the membership and lists the assignments that wait for a new "
        "owner. Deletes nothing. Never changes the person's company account. "
        "Needs the tenant_admin role.",
        s.DISABLE_PARAMETERS,
        s.DISABLE_RESPONSE,
        s.DISABLE_GUIDANCE,
        _WRITE_TAGS,
        "caution",
    ),
    (
        "spaces.overview",
        spaces_overview,
        "Show spaces with owners, members, product versions and health.",
        "Reads stored data only. Per space: owner, members, assigned systems; "
        "per target its detected product and version and its health with the "
        "check time. Makes no vendor call.",
        s.OVERVIEW_PARAMETERS,
        s.OVERVIEW_RESPONSE,
        s.OVERVIEW_GUIDANCE,
        ["spaces", "read", "ownership", "health"],
        "safe",
    ),
)

for _spec in _SPECS:
    if _spec[-1] == "caution":
        register_role_floor(_spec[0], TenantRole.TENANT_ADMIN)


async def register_spaces_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the five ``spaces.*`` typed operations. Safe to run twice."""
    for op_id, handler, summary, description, params, response, guidance, tags, safety in _SPECS:
        await register_typed_operation(
            product="spaces",
            version="1.x",
            impl_id="spaces-records",
            op_id=op_id,
            handler=handler,
            group_key=s.GROUP_KEY,
            when_to_use=s.GROUP_WHEN_TO_USE,
            summary=summary,
            description=description,
            parameter_schema=params,
            response_schema=response,
            tags=tags,
            safety_level=safety,
            requires_approval=safety != "safe",
            llm_instructions=guidance,
            embedding_service=embedding_service,
        )
