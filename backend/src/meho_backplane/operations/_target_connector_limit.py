# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the connector that a target picks against a connector limit.

The dispatcher picks the connector that runs a call from the target, not
from the ``connector_id`` of the call. Two limits use that picked
connector:

* **A hosted agent's connector list.** The agent toolset binds the list
  of the agent's allowed connectors (:func:`connector_limit`) around its
  ``call_operation`` and ``preview_operation`` tools. ``dispatch`` and
  ``preview_dispatch`` then refuse a call whose target picks a connector
  outside the list, or picks no single connector, with
  ``connector_not_allowed``. The check runs right after the
  target-to-connector check, before the policy gate: the refusal writes
  one audit row, makes no approval request, sends nothing to the target
  and publishes no broadcast. A step of a multi-step operation runs in the
  same context, so its target is checked too.
* **A standing grant of a service login.** A grant names one connector.
  :func:`target_picks_other_connector` tells the grant lookup when the
  target picks another connector, so the grant does not clear the call.

Outside an agent run with a connector list, the limit is unset and
``dispatch`` behaves as without this module.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult, resolve_connector_or_label
from meho_backplane.connectors.base import Connector
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._audit import audit_rejection_safe, with_audit_receipt
from meho_backplane.operations.deny_reasons import CONNECTOR_NOT_ALLOWED

__all__ = [
    "allowed_connectors_var",
    "connector_id_of",
    "connector_limit",
    "not_allowed_preview_envelope",
    "refuse_target_connector_not_allowed",
    "target_picks_other_connector",
]

#: The connector ids a hosted agent may use, or ``None`` for no limit.
allowed_connectors_var: ContextVar[frozenset[str] | None] = ContextVar(
    "allowed_connectors", default=None
)


@contextmanager
def connector_limit(allowed: frozenset[str] | None) -> Iterator[None]:
    """Bind *allowed* as the connector limit while the block runs."""
    token = allowed_connectors_var.set(allowed)
    try:
        yield
    finally:
        allowed_connectors_var.reset(token)


def connector_id_of(cls: type[Connector]) -> str:
    """The connector id of *cls*, in the form ``meho_connector_list`` shows."""
    if cls.impl_id and cls.version:
        return f"{cls.impl_id}-{cls.version}"
    return cls.product


async def _picked_connector(descriptor: EndpointDescriptor, target: Any) -> type[Connector] | None:
    """The connector class the dispatcher picks for *target*, or ``None``.

    An operation from an API file first loads the connectors other replicas
    built from saved API files, as the run step does, so both pick the same
    class.
    """
    if descriptor.source_kind == "ingested":
        # Imported lazily: the dispatcher imports this module at load time.
        from meho_backplane.operations.dispatcher import _restore_saved_ingest_profiles

        await _restore_saved_ingest_profiles(target)
    cls, label, _message = resolve_connector_or_label(target)
    if label is not None:
        return None
    return cls


def _target_label(target: Any) -> str:
    name = getattr(target, "name", None)
    return f"Target '{name}'" if isinstance(name, str) and name else "This target"


async def _not_allowed(
    descriptor: EndpointDescriptor, target: Any
) -> tuple[str, dict[str, Any]] | None:
    """The refusal text and extras when the limit refuses *target*, else ``None``."""
    allowed = allowed_connectors_var.get()
    if allowed is None or target is None:
        return None
    cls = await _picked_connector(descriptor, target)
    picked = connector_id_of(cls) if cls is not None else None
    if picked is not None and picked in allowed:
        return None
    allowed_text = ", ".join(sorted(allowed)) or "none"
    if picked is None:
        message = (
            f"{_target_label(target)} does not resolve to one connector, so MEHO "
            f"cannot check it against this agent's allowed connectors ({allowed_text})."
        )
    else:
        message = (
            f"{_target_label(target)} runs on connector {picked}, which is not in "
            f"this agent's allowed connectors ({allowed_text}). Use a target of an "
            "allowed connector."
        )
    extras = {
        "error_code": CONNECTOR_NOT_ALLOWED,
        "op_id": descriptor.op_id,
        "target_connector_id": picked,
        "allowed_connectors": sorted(allowed),
    }
    return f"{CONNECTOR_NOT_ALLOWED}: {message}", extras


async def refuse_target_connector_not_allowed(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params_hash: str,
    started: float,
) -> OperationResult | None:
    """The audited ``connector_not_allowed`` result, or ``None`` when the target fits."""
    refusal = await _not_allowed(descriptor, target)
    if refusal is None:
        return None
    error, extras = refusal
    duration_ms = (time.monotonic() - started) * 1000.0
    receipt = await audit_rejection_safe(
        audit_id=uuid.uuid4(),
        operator=operator,
        descriptor=descriptor,
        target=target,
        params_hash=params_hash,
        result_status=CONNECTOR_NOT_ALLOWED,
        duration_ms=duration_ms,
    )
    result = OperationResult(
        status="error",
        op_id=descriptor.op_id,
        error=error,
        duration_ms=duration_ms,
        extras=extras,
    )
    return with_audit_receipt(result, receipt)


async def not_allowed_preview_envelope(
    *, descriptor: EndpointDescriptor, connector_id: str, target: Any
) -> dict[str, Any] | None:
    """The preview error envelope for a refused target, or ``None``. Writes no audit row."""
    refusal = await _not_allowed(descriptor, target)
    if refusal is None:
        return None
    error, extras = refusal
    return {
        "status": "error",
        "op_id": descriptor.op_id,
        "connector_id": connector_id,
        "source_kind": descriptor.source_kind,
        "error": error,
        "extras": extras,
    }


async def target_picks_other_connector(
    descriptor: EndpointDescriptor, target: Any, connector_id: str
) -> str | None:
    """The id of the connector *target* picks when it is not *connector_id*, else ``None``.

    ``None`` also when there is no target, or the target resolves no single
    connector: the run step answers ``no_connector`` or
    ``ambiguous_connector`` for it.
    """
    if target is None:
        return None
    cls = await _picked_connector(descriptor, target)
    if cls is None:
        return None
    picked = connector_id_of(cls)
    return None if picked == connector_id else picked
