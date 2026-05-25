# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas for the approval surfacing channel (G11.2-T5 / #818).

Wire contracts shared by the REST routes
(:mod:`meho_backplane.api.v1.approvals`), MCP tools
(:mod:`meho_backplane.mcp.tools.approvals`), and the Go CLI verbs
(``cli/internal/cmd/approvals``). All shapes use ``frozen=True`` and
``extra="forbid"`` so instances are safe to cache and unknown fields
surface as 422 rather than silently disappearing.

MCP elicitation URL-mode
------------------------

The **forward surfacing format** for in-loop approval is MCP elicitation
(2025-11-25 spec, URL mode). When a paused agent run reaches an MCP
client that supports elicitation, the approved wire shape is:

.. code-block:: json

    {
      "method": "elicitation/create",
      "params": {
        "message": "Approval required: <connector_id> / <op_id>",
        "requestedSchema": {
          "type": "object",
          "properties": {
            "decision": {
              "type": "string",
              "enum": ["approve", "reject"],
              "description": "Your decision on the proposed operation."
            },
            "reason": {
              "type": "string",
              "description": "Optional rationale for the decision."
            }
          },
          "required": ["decision"]
        }
      }
    }

The response carries ``action: "accept"`` and the operator's structured
``content``. MEHO surfaces the approval URL via the ``elicitation_url``
field on :class:`ApprovalRequestDetail` so MCP clients that support URL
mode can open the operator's decision UI directly:

    ``<backplane_base>/api/v1/approvals/{id}/decide``

See https://workos.com/blog/mcp-elicitation for the spec reference.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.db.models import ApprovalStatus

__all__ = [
    "ApprovalDecision",
    "ApprovalListResponse",
    "ApprovalRequestDetail",
    "ApprovalRequestSummary",
]


class ApprovalRequestSummary(BaseModel):
    """Compact row for list responses — enough to triage without the full detail."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    status: ApprovalStatus
    connector_id: str
    op_id: str
    principal_sub: str
    principal_act: str | None
    created_at: datetime
    expires_at: datetime | None


class ApprovalRequestDetail(BaseModel):
    """Full detail for show/inspect responses.

    ``elicitation_url`` is the MCP elicitation URL-mode forward wire
    address: ``<backplane_base>/api/v1/approvals/{id}/decide``. Clients
    that support MCP elicitation can POST the operator's structured
    decision directly to this URL.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    status: ApprovalStatus
    agent_run_id: uuid.UUID | None
    connector_id: str
    op_id: str
    target_id: uuid.UUID | None
    params_hash: str
    proposed_effect: dict[str, object] | None
    principal_sub: str
    principal_act: str | None
    reviewed_by: str | None
    decided_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    request_audit_id: uuid.UUID | None
    decision_audit_id: uuid.UUID | None
    # MCP elicitation URL-mode forward wire address.
    elicitation_url: str | None = Field(
        default=None,
        description=(
            "MCP elicitation URL-mode address for this request. "
            "POST a decision payload ({decision: 'approve'|'reject', reason?: string}) "
            "here to resolve the approval inline. "
            "See https://workos.com/blog/mcp-elicitation for the wire format."
        ),
    )


class ApprovalListResponse(BaseModel):
    """Paginated list of approval requests."""

    model_config = ConfigDict(frozen=True)

    items: list[ApprovalRequestSummary]
    total: int
    limit: int
    offset: int


class ApprovalDecision(BaseModel):
    """Approve or reject decision body.

    Posted to ``POST /api/v1/approvals/{id}/approve`` (or ``/reject``)
    and to the MCP elicitation URL-mode endpoint
    ``POST /api/v1/approvals/{id}/decide``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str | None = Field(
        default=None,
        description="Optional human-readable rationale for the decision.",
    )
