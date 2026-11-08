# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Refuse a connector-bound operation on a target of another kind (#5948).

A typed or composite operation whose handler is a method of a connector
class runs on the connector instance the resolver picks for the target.
When that connector does not have the handler (for example a linux
operation called on an rke2 target), the handler cannot be bound and the
call cannot run. Without this check the call went through the policy gate
and a possible wait for approval, then failed with an internal
"still unbound" error that did not say what was wrong.

:func:`target_connector_mismatch` finds the case. ``dispatch`` refuses
the call with ``target_connector_mismatch`` right after the target check,
before the policy gate, and writes one audit row
(:func:`refuse_target_connector_mismatch`). ``preview_dispatch`` returns
the same error without an audit row (:func:`mismatch_preview_envelope`).

A module-level handler (no ``self``) has no connector class, so it is
never refused here. A target that resolves no connector, or more than one,
is left to the ``no_connector`` / ``ambiguous_connector`` answers of the
connector step.
"""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult, resolve_connector_or_label
from meho_backplane.connectors.base import Connector
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._audit import audit_rejection_safe, with_audit_receipt
from meho_backplane.operations._handler_resolve import import_handler, is_unbound_method

__all__ = [
    "TARGET_CONNECTOR_MISMATCH",
    "TargetConnectorMismatch",
    "mismatch_preview_envelope",
    "refuse_target_connector_mismatch",
    "target_connector_mismatch",
]

TARGET_CONNECTOR_MISMATCH: Final = "target_connector_mismatch"


@dataclass(frozen=True)
class TargetConnectorMismatch:
    """Why the connector picked for the target cannot run the operation."""

    message: str
    target_product: str | None
    operation_product: str
    target_connector_id: str
    operation_connector_id: str

    def extras(self, op_id: str) -> dict[str, Any]:
        """The ``extras`` of the error answer."""
        return {
            "error_code": TARGET_CONNECTOR_MISMATCH,
            "op_id": op_id,
            "target_product": self.target_product,
            "operation_product": self.operation_product,
            "target_connector_id": self.target_connector_id,
            "operation_connector_id": self.operation_connector_id,
        }


def _self_first_handler(handler_ref: str) -> Callable[..., Any] | None:
    """The handler of *handler_ref* when it is a connector method, else ``None``.

    An unimportable handler returns ``None``: the ``handler_unreachable``
    answer of the run step reports it.
    """
    try:
        handler = import_handler(handler_ref)
        params = list(inspect.signature(handler).parameters)
    except (ImportError, TypeError, ValueError):
        return None
    if params and params[0] == "self":
        return handler
    return None


def _message(
    descriptor: EndpointDescriptor,
    target: Any,
    target_product: str | None,
    target_connector_id: str,
    operation_connector_id: str,
) -> str:
    op_id = descriptor.op_id
    op_product = descriptor.product
    name = getattr(target, "name", None)
    target_label = f"Target '{name}'" if isinstance(name, str) and name else "This target"
    if target_product and target_product != op_product:
        return (
            f"{target_label} is of kind {target_product}. {op_id} is an operation of "
            f"kind {op_product} and needs a target of kind {op_product}. Use a target "
            f"with product {op_product}, or pick an operation of kind {target_product} "
            f"(search_operations on connector {target_connector_id})."
        )
    return (
        f"{target_label} runs on connector {target_connector_id}. {op_id} belongs to "
        f"connector {operation_connector_id} and does not run there. Set the target's "
        f"preferred_impl_id to {descriptor.impl_id}, or pick an operation of connector "
        f"{target_connector_id} (search_operations)."
    )


def target_connector_mismatch(
    descriptor: EndpointDescriptor, target: Any
) -> TargetConnectorMismatch | None:
    """The mismatch when the target's connector cannot run *descriptor*, else ``None``.

    Only a typed or composite op with a connector-method handler and a
    target can be refused. The handler fits when it is defined anywhere on
    the picked class's MRO (a base class or a subclass instance both bind,
    see :func:`is_unbound_method`).
    """
    if descriptor.source_kind not in ("typed", "composite") or target is None:
        return None
    handler = _self_first_handler(descriptor.handler_ref or "")
    if handler is None:
        return None
    cls, label, _message_text = resolve_connector_or_label(target)
    if label is not None or cls is None:
        return None
    if is_unbound_method(handler, cls):
        return None
    return _mismatch(descriptor, target, cls)


def _mismatch(
    descriptor: EndpointDescriptor, target: Any, cls: type[Connector]
) -> TargetConnectorMismatch:
    raw_product = getattr(target, "product", None)
    target_product = raw_product if isinstance(raw_product, str) and raw_product else None
    target_connector_id = f"{cls.impl_id}-{cls.version}"
    operation_connector_id = f"{descriptor.impl_id}-{descriptor.version}"
    return TargetConnectorMismatch(
        message=_message(
            descriptor, target, target_product, target_connector_id, operation_connector_id
        ),
        target_product=target_product,
        operation_product=descriptor.product,
        target_connector_id=target_connector_id,
        operation_connector_id=operation_connector_id,
    )


async def refuse_target_connector_mismatch(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params_hash: str,
    started: float,
) -> OperationResult | None:
    """The audited ``target_connector_mismatch`` result, or ``None`` when the op fits."""
    mismatch = target_connector_mismatch(descriptor, target)
    if mismatch is None:
        return None
    duration_ms = (time.monotonic() - started) * 1000.0
    receipt = await audit_rejection_safe(
        audit_id=uuid.uuid4(),
        operator=operator,
        descriptor=descriptor,
        target=target,
        params_hash=params_hash,
        result_status=TARGET_CONNECTOR_MISMATCH,
        duration_ms=duration_ms,
    )
    result = OperationResult(
        status="error",
        op_id=descriptor.op_id,
        error=f"{TARGET_CONNECTOR_MISMATCH}: {mismatch.message}",
        duration_ms=duration_ms,
        extras=mismatch.extras(descriptor.op_id),
    )
    return with_audit_receipt(result, receipt)


def mismatch_preview_envelope(
    *, descriptor: EndpointDescriptor, connector_id: str, target: Any
) -> dict[str, Any] | None:
    """The preview error envelope for a mismatch, or ``None``. Writes no audit row."""
    mismatch = target_connector_mismatch(descriptor, target)
    if mismatch is None:
        return None
    return {
        "status": "error",
        "op_id": descriptor.op_id,
        "connector_id": connector_id,
        "source_kind": descriptor.source_kind,
        "error": f"{TARGET_CONNECTOR_MISMATCH}: {mismatch.message}",
        "extras": mismatch.extras(descriptor.op_id),
    }
