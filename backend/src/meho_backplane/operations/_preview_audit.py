# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row of a preview that read its target.

Most previews never contact the target, and the request wrapper records them
as before. A preview whose builder may read the target
(:func:`~meho_backplane.operations._preview.register_preview_target_read`,
for example the dry run of ``k8s.apply`` with ``K8S_APPLY_PREVIEW_DRY_RUN``
on) did contact it. So :func:`write_target_read_row` writes one row for it,
with or without ``AUDIT_WRAPPER_ROWS_RETIRED``:

* ``event_class`` ``operation_execution``: a request went to the target;
* ``method`` ``PREVIEW`` and ``path`` the op id, so the row is never read as
  a run of the operation itself (a run has ``method`` ``DISPATCH``);
* ``payload.op_class`` ``read``, ``payload.preview`` ``target_read``, the
  ``result_status`` (``ok``, ``timeout``, ``error``, or ``cancelled`` when
  the caller was cancelled while it waited for the target) and the fields the op
  adds (for ``k8s.apply`` the number of documents);
* the caller, the tenant and the target, as on every row of a call.

It is the caller's own classified row, so with the wrapper rows retired the
request wrapper adds no row (``audit_cutover``, rule 1). With them kept, the
wrapper writes its ``legacy_request`` row as before.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import AuditEventClass, EndpointDescriptor
from meho_backplane.operations._audit import (
    _resolve_target_id,
    _run_to_end,
    _store_row,
    resolve_audit_session_id,
)
from meho_backplane.operations.audit_row_safe import clamp_duration_ms
from meho_backplane.request_usage.collector import current_request_id

__all__ = ["TARGET_READ_METHOD", "write_target_read_row"]

#: The ``method`` of the row: a preview, not a run of the operation.
TARGET_READ_METHOD = "PREVIEW"

#: The HTTP-like status of the row for each result of the read.
_STATUS_CODES: dict[str, int] = {"ok": 200, "timeout": 504, "error": 502, "cancelled": 499}


async def write_target_read_row(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    result_status: str,
    duration_ms: float,
    fields: dict[str, Any],
) -> uuid.UUID:
    """Write the one audit row of a preview that read its target; return its id.

    Raises when the row cannot be stored: the preview then fails, as a call
    fails when its audit row does not commit.
    """
    audit_id = uuid.uuid4()
    core: dict[str, Any] = {
        "op_id": descriptor.op_id,
        "op_class": "read",
        "preview": "target_read",
        "source_kind": descriptor.source_kind,
        "connector_product": descriptor.product,
        "connector_version": descriptor.version,
        "connector_impl_id": descriptor.impl_id,
        "result_status": result_status,
        **fields,
    }
    row_fields: dict[str, Any] = {
        "id": audit_id,
        "occurred_at": datetime.now(UTC),
        "operator_sub": operator.sub,
        "actor_sub": resolve_actor_sub(),
        "tenant_id": operator.tenant_id,
        "target_id": _resolve_target_id(target),
        "agent_session_id": resolve_audit_session_id(),
        "method": TARGET_READ_METHOD,
        "path": descriptor.op_id,
        "status_code": _STATUS_CODES.get(result_status, 502),
        "request_id": current_request_id(),
        "duration_ms": clamp_duration_ms(duration_ms),
    }
    await _run_to_end(
        _store_row(
            row_fields=row_fields,
            event_class=AuditEventClass.OPERATION_EXECUTION,
            operator=operator,
            core=core,
            payload=dict(core),
            raw_payload=None,
            redaction_manifest=None,
            error_extras=None,
        ),
        audit_id=audit_id,
    )
    return audit_id
