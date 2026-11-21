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
  ``connector_not_allowed``. The check runs right after the target
  check, before the mismatch check and the policy gate: the refusal writes
  one audit row, makes no approval request, sends nothing to the target
  and publishes no broadcast. A step of a multi-step operation runs in the
  same context, so its target is checked too. A call that waits for
  approval stores the list on its request (:func:`stored_connector_limit`).
  The run after the approval binds it again
  (:func:`restored_connector_limit`), also when a person approves the
  request after the agent stopped waiting.
* **A standing grant of a service login.** A grant names one connector.
  :func:`target_picks_other_connector` tells the grant lookup when the
  target picks another connector, so the grant does not clear the call.

The limit check comes before the ``target_connector_mismatch`` check, and
its refusal names only the connectors in the list. So a refusal to a
caller with a list names no connector outside the list.

The same list limits what a hosted agent reads back:

* **Stored results.** While the list is bound, every stored result keeps
  it (:data:`~meho_backplane.connectors.result_handle_store.result_connector_scope_var`).
  A read under a list finds only a result stored under a list inside its
  own (:func:`stored_result_within_limit`). Any other result is a miss.
* **A stored request.** :func:`stored_request_outside_limit` tells the
  reveal fetch when a stored approval request is for a connector, or a
  target of a connector, outside the list.
* **Targets.** :func:`targets_within_limit` keeps only the targets whose
  connector is in the list. ``list_targets`` and the near-match targets of
  a ``no_target`` or ``ambiguous_target`` answer use it.

Outside an agent run with a connector list, the limit is unset and
``dispatch`` behaves as without this module.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult, resolve_connector_or_label
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.result_handle_store import result_connector_scope_var
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations._audit import audit_rejection_safe, with_audit_receipt
from meho_backplane.operations.deny_reasons import CONNECTOR_NOT_ALLOWED

__all__ = [
    "allowed_connectors_var",
    "connector_id_of",
    "connector_limit",
    "not_allowed_preview_envelope",
    "refuse_target_connector_not_allowed",
    "restored_connector_limit",
    "stored_connector_limit",
    "stored_request_outside_limit",
    "stored_result_within_limit",
    "target_picks_other_connector",
    "targets_within_limit",
]

#: The connector ids a hosted agent may use, or ``None`` for no limit.
allowed_connectors_var: ContextVar[frozenset[str] | None] = ContextVar(
    "allowed_connectors", default=None
)


@contextmanager
def connector_limit(allowed: frozenset[str] | None) -> Iterator[None]:
    """Bind *allowed* as the connector limit while the block runs.

    Every result stored while the block runs keeps the list too.
    """
    token = allowed_connectors_var.set(allowed)
    scope_token = result_connector_scope_var.set(sorted(allowed) if allowed is not None else None)
    try:
        yield
    finally:
        result_connector_scope_var.reset(scope_token)
        allowed_connectors_var.reset(token)


def stored_connector_limit() -> list[str] | None:
    """The bound limit as the sorted list an approval request stores, or ``None``."""
    allowed = allowed_connectors_var.get()
    return sorted(allowed) if allowed is not None else None


@contextmanager
def restored_connector_limit(stored: list[str] | None) -> Iterator[None]:
    """Bind the limit an approval request stored while its approved run runs.

    A request without a stored list keeps the limit that is bound already.
    """
    if stored is None:
        yield
        return
    with connector_limit(frozenset(stored)):
        yield


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
    return await _picked_connector_class(target, ingested=descriptor.source_kind == "ingested")


async def _picked_connector_class(target: Any, *, ingested: bool) -> type[Connector] | None:
    if ingested:
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
    # The answer names only the connectors in the list, never the one the
    # target picked.
    allowed_text = ", ".join(sorted(allowed)) or "none"
    if picked is None:
        message = (
            f"{_target_label(target)} does not resolve to one connector, so MEHO "
            f"cannot check it against this agent's allowed connectors ({allowed_text})."
        )
    else:
        message = (
            f"{_target_label(target)} does not run on a connector in this agent's "
            f"allowed connectors ({allowed_text}). Use a target of an allowed connector."
        )
    extras = {
        "error_code": CONNECTOR_NOT_ALLOWED,
        "op_id": descriptor.op_id,
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


def stored_result_within_limit(scope: list[str] | None, allowed: frozenset[str] | None) -> bool:
    """True when a stored result with connector list *scope* may be read under *allowed*.

    *allowed* is the reader's limit (:data:`allowed_connectors_var`, read by
    the caller on the event loop: the check may run on a worker thread).
    Without a limit every result may be read. With one, only a result
    stored under a list that is inside it: a result stored without a list
    (for example by the person who started the run) may hold data of any
    connector.
    """
    if allowed is None:
        return True
    return scope is not None and set(scope) <= allowed


async def stored_request_outside_limit(operator: Operator, request: ApprovalRequest) -> bool:
    """True when the bound limit refuses the stored approval *request*.

    The request is refused when its connector is not in the list, or its
    stored target picks a connector outside the list, picks no single
    connector, or MEHO finds no target with the stored id. Without a bound
    limit, nothing is refused.
    """
    allowed = allowed_connectors_var.get()
    if allowed is None:
        return False
    if request.connector_id not in allowed:
        return True
    if request.target_id is None:
        return False
    # Imported lazily: the target resolver imports the operations package.
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.targets.resolver import resolve_target_by_id

    async with get_sessionmaker()() as session:
        target = await resolve_target_by_id(session, operator.tenant_id, request.target_id)
    if target is None:
        return True
    # The op of the request may come from an API file, so load the saved
    # connectors first, as the run step does for such an op.
    cls = await _picked_connector_class(target, ingested=True)
    return cls is None or connector_id_of(cls) not in allowed


async def targets_within_limit[T](targets: Iterable[T]) -> list[T]:
    """The *targets* whose connector the bound limit allows, in their order.

    Without a bound limit every target is kept. With one, a target is kept
    only when it picks one connector and that connector is in the list. A
    target that picks no single connector is left out.
    """
    allowed = allowed_connectors_var.get()
    if allowed is None:
        return list(targets)
    kept: list[T] = []
    for target in targets:
        # A target may use a connector built from a saved API file, so load
        # those first, as the run step does.
        cls = await _picked_connector_class(target, ingested=True)
        if cls is not None and connector_id_of(cls) in allowed:
            kept.append(target)
    return kept
