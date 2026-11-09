# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Record a call refused with ``license_read_off`` in the audit log (#5791).

MEHO never runs an ingested read of licence keys
(:mod:`meho_backplane.operations.license_reads`). ``dispatch`` refuses such
a call before the target check, the catalog guard, the policy gate and any
vendor call. The refusal is a decision about the call, so it gets one
``policy_decision`` row, like a policy denial: the row names the op id, has
``result_status='denied'`` and holds the refusal code under
``payload["error"]["error_code"]``. Nothing ran, so the row has no result.

Two dispatch paths answer ``license_read_off``:

* the row is turned on: the dispatcher has the real descriptor;
* the row is off (the usual case): the lookup found no enabled row, and
  :func:`~meho_backplane.operations._op_miss.op_miss_result` saw that the
  caller can see the row in any state. Then the row is written for an
  in-memory descriptor with the same connector and op id. It is never
  added to a session.

A preview of the call (``preview_operation``) writes no audit row, as
every preview.
"""

from __future__ import annotations

import uuid
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.db.models import AuditEventClass, EndpointDescriptor
from meho_backplane.operations._audit import audit_safe, with_audit_receipt

__all__ = ["LICENSE_READ_OFF", "audit_license_read_off", "license_read_stand_in"]

#: The ``error_code`` of the refusal.
LICENSE_READ_OFF = "license_read_off"


def license_read_stand_in(
    *, product: str, version: str, impl_id: str, op_id: str
) -> EndpointDescriptor:
    """An in-memory descriptor for an ingested licence read that is off.

    It carries only what the audit row reads: the connector triple, the op
    id and ``source_kind='ingested'`` (only an ingested read is refused
    this way). Never added to a session.
    """
    return EndpointDescriptor(
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        source_kind="ingested",
        parameter_schema={},
    )


async def audit_license_read_off(
    result: OperationResult,
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params_hash: str,
) -> OperationResult:
    """Write the ``policy_decision`` row for *result*; return it with the row id.

    The write is fail-open, like every refusal row: when it fails, the
    error is logged and the caller still gets the refusal, without an
    ``audit_id``.
    """
    receipt = await audit_safe(
        audit_id=uuid.uuid4(),
        event_class=AuditEventClass.POLICY_DECISION,
        operator=operator,
        descriptor=descriptor,
        target=target,
        params_hash=params_hash,
        result_status="denied",
        duration_ms=result.duration_ms,
        error_extras=dict(result.extras) if result.extras else None,
    )
    return with_audit_receipt(result, receipt)
