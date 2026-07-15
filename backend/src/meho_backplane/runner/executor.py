# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Execute one centrally-authorized work item against a local handler.

The runner reuses the chassis's DB-free handler-resolution primitives
(:mod:`meho_backplane.operations._handler_resolve`) but not the DB-bound
:func:`~meho_backplane.operations.dispatcher.dispatch`: the assignment
already carries the centrally-resolved descriptor fields, so the executor
resolves the handler from the payload alone.

Two fail-closed guards make the runner a strictly bounded executor
(defence in depth — central mint is the real authorization boundary,
owned by #2500):

* **safe-only** — any item whose ``safety_level`` is not ``"safe"`` is
  refused without invoking anything (v1 authorizes read-only workloads).
* **connector-tree-only** — a ``handler_ref`` that does not resolve inside
  ``meho_backplane.connectors.*`` is refused. The lexical prefix is
  checked *before* import (an import has module-load side effects) and the
  resolved callable's ``__module__`` is re-checked after.

A handler that raises becomes a structured ``error`` result — a failed
check is a result, never a crashed tick.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    import_handler,
    is_unbound_method,
)
from meho_backplane.runner.wire import RunnerResult, RunnerWorkItem

__all__ = ["execute_work_item"]

_log = structlog.get_logger(__name__)

_ALLOWED_SAFETY_LEVEL = "safe"
_CONNECTOR_MODULE_PREFIX = "meho_backplane.connectors."


def _result(
    item: RunnerWorkItem,
    status: str,
    *,
    payload: dict[str, Any] | None,
    error: str | None,
) -> RunnerResult:
    return RunnerResult(
        result_uid=uuid.uuid4().hex,
        check_ref=item.check_ref,
        op_id=item.op_id,
        status=status,
        result=payload,
        error=error,
    )


def _screen_item(item: RunnerWorkItem) -> str | None:
    """Pre-import fail-closed screen: a refusal reason, or ``None`` to proceed.

    Both checks run before any import so an out-of-tree ``handler_ref``
    never triggers a module-load side effect.
    """
    if item.safety_level != _ALLOWED_SAFETY_LEVEL:
        return f"safety_level {item.safety_level!r} refused; runner executes only 'safe' ops"
    if not item.handler_ref.startswith(_CONNECTOR_MODULE_PREFIX):
        return f"handler_ref {item.handler_ref!r} is outside {_CONNECTOR_MODULE_PREFIX}*"
    return None


async def _invoke(handler: Callable[..., Awaitable[Any]], item: RunnerWorkItem) -> RunnerResult:
    """Invoke *handler* and wrap its outcome as a structured result."""
    operator = _build_operator(item)
    try:
        payload = await handler(operator, item.target_descriptor, dict(item.params))
    except Exception as exc:  # a failed check is a result, never a crashed tick
        _log.warning(
            "runner_item_handler_raised",
            op_id=item.op_id,
            check_ref=item.check_ref,
            exc_info=True,
        )
        return _result(item, "error", payload=None, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(payload, dict):
        return _result(
            item, "error", payload=None, error=f"handler returned non-dict {type(payload).__name__}"
        )
    return _result(item, "ok", payload=payload, error=None)


async def execute_work_item(item: RunnerWorkItem) -> RunnerResult:
    """Execute *item* locally and return a structured :class:`RunnerResult`."""
    refusal = _screen_item(item)
    if refusal is not None:
        _log.warning(
            "runner_item_refused",
            op_id=item.op_id,
            check_ref=item.check_ref,
            reason=refusal,
        )
        return _result(item, "refused", payload=None, error=refusal)

    try:
        handler = import_handler(item.handler_ref)
    except (ImportError, TypeError) as exc:
        _log.warning(
            "runner_item_handler_unresolved",
            op_id=item.op_id,
            check_ref=item.check_ref,
            handler_ref=item.handler_ref,
        )
        return _result(item, "error", payload=None, error=f"handler_unresolved: {exc}")

    module = getattr(handler, "__module__", "") or ""
    if not module.startswith(_CONNECTOR_MODULE_PREFIX):
        _log.warning(
            "runner_item_refused_out_of_tree",
            op_id=item.op_id,
            check_ref=item.check_ref,
            handler_ref=item.handler_ref,
            resolved_module=module,
        )
        return _result(
            item,
            "refused",
            payload=None,
            error=f"handler_ref {item.handler_ref!r} resolved outside {_CONNECTOR_MODULE_PREFIX}*",
        )

    return await _invoke(_maybe_bind_method(handler, item), item)


def _maybe_bind_method(
    handler: Callable[..., Awaitable[Any]], item: RunnerWorkItem
) -> Callable[..., Awaitable[Any]]:
    """Rebind a bound-method handler against its connector instance.

    Mirrors the dispatcher's rebinding (``_maybe_bind_method``) minus the
    DB descriptor lookup: the connector class comes from the in-memory
    registry keyed on the payload's ``(product, version, impl_id)``.
    Module-level function handlers (e.g. ``net.*``) are not on any
    connector's MRO and are returned unchanged.
    """
    connector_cls = all_connectors_v2().get((item.product, item.version, item.impl_id))
    if connector_cls is None:
        return handler
    if not is_unbound_method(handler, connector_cls):
        return handler
    instance = get_or_create_connector_instance(connector_cls)
    bound: Callable[..., Awaitable[Any]] = handler.__get__(instance, connector_cls)
    return bound


def _build_operator(item: RunnerWorkItem) -> Operator:
    """Reconstruct the acting :class:`Operator` from the principal context.

    ``raw_jwt`` is empty: the op was authorized centrally, so no bearer
    token for the acting principal exists on the runner, and the field is
    ``repr``-excluded so it never leaks even when empty.
    """
    principal = item.principal
    return Operator(
        sub=principal.sub,
        raw_jwt="",
        tenant_id=principal.tenant_id,
        tenant_role=principal.tenant_role,
        principal_kind=principal.principal_kind,
    )
