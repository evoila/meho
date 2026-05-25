# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval surfacing channel — G11.2-T5 (#818).

The ``approvals`` package owns the operator-facing surface for
pending approval requests:

* :mod:`~meho_backplane.approvals.schemas` — Pydantic shapes for the
  REST / MCP / CLI wire contracts.
* :mod:`~meho_backplane.approvals.service` — :class:`ApprovalRequestService`,
  the single code path REST routes, MCP tools, and Go CLI verbs
  all dispatch through; enforces tenant boundaries and RBAC invariants.
"""

from meho_backplane.approvals.service import (
    ApprovalDecisionError,
    ApprovalNotFoundError,
    ApprovalRequestService,
)

__all__ = [
    "ApprovalDecisionError",
    "ApprovalNotFoundError",
    "ApprovalRequestService",
]
