# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Review report for stored ingested deletes below ``destructive`` (#3827).

A corrected parser default or connector floor changes nothing for rows an
earlier ingest already stored: they keep their old ``safety_level`` and
``requires_approval`` until the connector is ingested again. This report
lets an operator see those rows on ``GET /api/v1/connectors/{id}/review``
(and ``meho connector review``) before one of them is hit live.

For every stored ingested write below ``destructive`` the report recomputes
the posture a fresh ingest would assign today -- the parser default
(:func:`~meho_backplane.operations.ingest.openapi.default_safety_posture`),
then the connector floor and the catalog overlay
(:func:`~meho_backplane.operations.ingest.safety_floors.apply_safety_floor`)
-- and keeps the row when it is delete-shaped: a ``DELETE`` verb, a write
the floor treats as a removal (the vCenter tag detach actions), or a vendor
action name that means delete (``POST:/VirtualMachine/{moId}/Destroy_Task``;
the word list in :mod:`meho_backplane.delete_names`, #4632). A row is
flagged ``needs_correction`` when it is weaker than that recomputed posture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_backplane.delete_names import destructive_action_name_in_op_id
from meho_backplane.operations.ingest.openapi import default_safety_posture
from meho_backplane.operations.ingest.payload import ConnectorReviewDeleteOp
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto, SafetyLevel

if TYPE_CHECKING:
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations.ingest._internals import ConnectorScope

__all__ = ["build_delete_review"]

_SAFETY_RANK: dict[str, int] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}


def _expected_posture(
    row: EndpointDescriptor, scope: ConnectorScope, method: str, path: str
) -> tuple[SafetyLevel, bool]:
    """What a fresh ingest of ``(method, path)`` would store today."""
    safety_level, requires_approval = default_safety_posture(method, path=path)
    proto = EndpointDescriptorProto(
        op_id=row.op_id,
        method=method,
        path=path,
        parameter_schema={"type": "object", "properties": {}},
        safety_level=safety_level,
        requires_approval=requires_approval,
    )
    floored = apply_safety_floor(
        product=scope.product, version=scope.version, impl_id=scope.impl_id, proto=proto
    )
    return floored.safety_level, floored.requires_approval


def _reason(
    *, safety_level: str, requires_approval: bool, expected_level: str, expected_approval: bool
) -> tuple[bool, str]:
    """Return ``(needs_correction, plain-English reason)`` for one row."""
    if _SAFETY_RANK[safety_level] < _SAFETY_RANK[expected_level]:
        return True, (
            f"stored at {safety_level}; a fresh ingest gives {expected_level}. "
            "Ingest the connector again, or correct the row with edit-op."
        )
    if expected_approval and not requires_approval:
        return True, (
            "runs at once for a human operator; a fresh ingest makes it wait for "
            "approval. Ingest the connector again, or correct the row with edit-op."
        )
    if requires_approval:
        return False, (
            f"waits for approval at {safety_level}; below destructive, so no preview "
            "of the affected objects is required before approval."
        )
    return False, f"stored at {safety_level} without approval; a fresh ingest gives the same."


def build_delete_review(
    rows: list[EndpointDescriptor], scope: ConnectorScope
) -> list[ConnectorReviewDeleteOp]:
    """Render the stored-delete review rows for *rows* (already below ``destructive``)."""
    report: list[ConnectorReviewDeleteOp] = []
    for row in rows:
        method = (row.method or "").upper()
        path = row.path or ""
        if not method or not path:
            continue
        expected_level, expected_approval = _expected_posture(row, scope, method, path)
        if (
            method != "DELETE"
            and expected_level != "destructive"
            and destructive_action_name_in_op_id(row.op_id) is None
        ):
            continue
        needs_correction, reason = _reason(
            safety_level=row.safety_level,
            requires_approval=row.requires_approval,
            expected_level=expected_level,
            expected_approval=expected_approval,
        )
        report.append(
            ConnectorReviewDeleteOp(
                op_id=row.op_id,
                method=method,
                safety_level=row.safety_level,
                requires_approval=row.requires_approval,
                is_enabled=row.is_enabled,
                expected_safety_level=expected_level,
                expected_requires_approval=expected_approval,
                needs_correction=needs_correction,
                reason=reason,
            )
        )
    return report
