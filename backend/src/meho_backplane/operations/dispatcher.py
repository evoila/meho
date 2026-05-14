# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``dispatch()`` -- the single entry point every operation flows through.

G0.6-T5 (#396) of Initiative #388. Every ``call_operation`` meta-tool,
every CLI alias verb, and every composite-handler-internal sub-call
routes through :func:`dispatch`. The function orchestrates the eight
phases the parent Initiative names:

1. Parse ``connector_id`` -> ``(product, version, impl_id)``
   (:func:`~meho_backplane.operations._lookup.parse_connector_id`).
2. Look up :class:`EndpointDescriptor` by the natural key
   (:func:`~meho_backplane.operations._lookup.lookup_descriptor`).
   Unknown -> structured ``unknown_op`` error.
3. Validate ``params`` against ``descriptor.parameter_schema`` via
   :class:`Draft202012Validator` (JSON Schema 2020-12, OpenAPI 3.1
   compatible) -- :func:`~meho_backplane.operations._validate.validate_params`.
   Invalid -> structured ``invalid_params`` error.
4. Policy gate (v0.2 default-allow;
   :func:`~meho_backplane.operations._validate.policy_gate`) --
   ``requires_approval=True`` -> ``denied``.
5. Resolve the connector class via
   :func:`~meho_backplane.connectors.resolver.resolve_connector` and
   instantiate it (cached at module level). Resolver miss ->
   structured ``no_connector`` error.
6. Branch on ``descriptor.source_kind`` -- ``ingested`` / ``typed`` /
   ``composite``. See :mod:`meho_backplane.operations._branches`.
7. JSONFlux-wrap the response via the :class:`Reducer` (v0.2 default
   is :class:`~meho_backplane.operations.reducer.PassThroughReducer`;
   T6 #397 ships the real reduction).
8. Write the audit row synchronously + publish a broadcast event
   (:func:`~meho_backplane.operations._audit.audit_and_broadcast_safe`).
9. Return the :class:`OperationResult`.

The dispatch function is async; safe to call from FastAPI routes, MCP
tool handlers, and from composite handlers (recursive).

Error contract
==============

The dispatcher never raises; it always returns an :class:`OperationResult`.
The error-shaped exit points carry structured ``error`` strings of the
form ``"<code>: <human-readable>"`` so callers can both string-match
(``error.startswith("unknown_op:")``) and parse the suffix for display.
Detail payloads land in ``extras``. Codes:

* ``unknown_op`` -- the natural key didn't resolve a descriptor.
* ``invalid_params`` -- params failed JSON Schema validation.
* ``no_connector`` -- resolver couldn't pick a connector for the target.
* ``handler_unreachable`` -- ``importlib`` couldn't resolve
  ``handler_ref``, or the resolved symbol is not callable.
* ``denied`` -- the policy gate denied the call.
* ``connector_error`` -- the connector / handler raised. The raised
  exception's class name lands in ``extras["exception_class"]``;
  the (length-capped) message in ``extras["exception_message"]``.

Why "always return, never raise"
================================

Two distinct dispatch surfaces consume this function:

* HTTP routes via FastAPI -- a raised exception turns into a 500 via
  the chassis exception handler. Useful for genuine programming bugs
  (the dispatcher tries to import a handler from a deleted module),
  not for user-input errors (bad params, unknown op-id).
* MCP tool handlers, CLI verbs, and recursive composite calls -- the
  caller wants a structured result it can render to the operator; a
  raised exception across the MCP JSON-RPC boundary turns into a
  generic 500 with no diagnostic surface.

Returning a structured :class:`OperationResult` for every operator-
visible failure mode keeps the contract uniform across the three call
sites; genuine programming bugs (DB connection drops, audit insert
failures) are caught + logged via
:func:`~meho_backplane.operations._audit.audit_and_broadcast_safe`'s
exception swallow, never crashing the dispatcher.

References
==========

* Parent Initiative -- #388 G0.6 (work item 5, "the heart of the substrate").
* Prerequisites -- #392 (tables + ORM), #393 (resolver), #394 (ABC
  metadata), #395 (typed-op registration helper).
* Audit row schema -- :class:`~meho_backplane.db.models.AuditLog`
  (extended in #351 with ``target_id``).
* Broadcast event schema -- :class:`~meho_backplane.broadcast.events.BroadcastEvent`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import (
    NoMatchingConnector,
    OperationResult,
    ResultHandle,
    resolve_connector,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._audit import (
    audit_and_broadcast_safe,
    parent_audit_id_var,
)
from meho_backplane.operations._branches import (
    dispatch_composite,
    dispatch_ingested,
    dispatch_typed,
)
from meho_backplane.operations._errors import (
    result_connector_error,
    result_denied,
    result_handler_unreachable,
    result_invalid_params,
    result_no_connector,
    result_unknown_op,
    wrap_ok_result,
)
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    import_handler,
    is_unbound_method,
    reset_connector_instance_cache,
    reset_handler_cache,
)
from meho_backplane.operations._lookup import (
    count_known_ops,
    lookup_descriptor,
    parse_connector_id,
)
from meho_backplane.operations._validate import (
    compute_params_hash,
    policy_gate,
    validate_params,
)
from meho_backplane.operations.composite import (
    CompositeRecursionLimitExceeded,
    DispatchChild,
    get_dispatch_child,
)
from meho_backplane.operations.reducer import (
    PassThroughReducer,
    Reducer,
)

__all__ = [
    "CompositeRecursionLimitExceeded",
    "DispatchChild",
    "Dispatcher",
    "compute_params_hash",
    "dispatch",
    "import_handler",
    "parent_audit_id_var",
    "reset_dispatcher_caches",
    "set_default_reducer",
]


# Module-level default reducer instance. T6 (#397) will swap in the
# real reducer via :func:`set_default_reducer` (or replace the module
# outright); T5 wires a pass-through so the dispatcher's reducer
# invocation point is exercised end-to-end today.
_DEFAULT_REDUCER: Reducer = PassThroughReducer()


def set_default_reducer(reducer: Reducer) -> None:
    """Replace the module-level default reducer.

    T6 ships the per-op reducer selection logic; this hook lets the
    integration test in T6 install a real reducer without monkeypatching
    the dispatcher import. Production callers leave the default alone.
    """
    global _DEFAULT_REDUCER
    _DEFAULT_REDUCER = reducer


def reset_dispatcher_caches() -> None:
    """Empty the handler-ref + connector-instance caches.

    Test-only -- production code relies on the lifetime caching to
    amortise the importlib walk and the connector instantiation cost.
    Tests that re-register handlers or swap connector classes between
    test functions call this to start each test from a clean slate.
    """
    reset_handler_cache()
    reset_connector_instance_cache()


#: Type alias for the dispatcher callable a composite handler receives.
#: Composite handlers take ``(operator, target, params, dispatch)``; the
#: ``dispatch`` argument is annotated with this alias so static type
#: checkers see the same signature as :func:`dispatch` itself without
#: forcing handlers to import the function for typing alone.
type Dispatcher = Callable[..., Awaitable[OperationResult]]


async def _resolve_connector_instance(
    descriptor: EndpointDescriptor,
    target: Any,
) -> tuple[Connector | None, str | None]:
    """Resolve a connector instance for *target* per ``descriptor.source_kind``.

    Returns ``(instance, error_reason)``:

    * ``(instance, None)`` -- resolver picked a class; instance is the
      cached singleton.
    * ``(None, None)`` -- no connector needed (typed/composite with a
      module-level handler, or no target).
    * ``(None, "no_connector")`` -- ingested op with no resolver match;
      the caller surfaces this as the ``no_connector`` error.

    Split out so the dispatcher's main body doesn't need three nested
    branches around resolver semantics.
    """
    if descriptor.source_kind == "ingested":
        try:
            connector_cls = resolve_connector(target)
        except NoMatchingConnector:
            return None, "no_connector"
        return get_or_create_connector_instance(connector_cls), None
    if descriptor.source_kind in ("typed", "composite") and target is not None:
        try:
            optional_cls = resolve_connector(target)
        except NoMatchingConnector:
            return None, None
        return get_or_create_connector_instance(optional_cls), None
    return None, None


def _maybe_bind_method(
    handler: Callable[..., Awaitable[Any]],
    connector_instance: Connector | None,
) -> Callable[..., Awaitable[Any]]:
    """Bind *handler* against *connector_instance* when it's an unbound method.

    :func:`import_handler` walks the dotted path via :func:`getattr`,
    which returns the **unbound** function for class-attribute lookups.
    Bound-method handlers need to be rebound against the connector
    instance the resolver chose so the dispatched call hits the right
    transport. Module-level handlers are returned unchanged.
    """
    if connector_instance is None:
        return handler
    if not is_unbound_method(handler, type(connector_instance)):
        return handler
    bound: Callable[..., Awaitable[Any]] = handler.__get__(
        connector_instance, type(connector_instance)
    )
    return bound


async def _run_source_kind_branch(
    *,
    descriptor: EndpointDescriptor,
    connector_instance: Connector | None,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    audit_id: uuid.UUID,
) -> Any:
    """Execute the descriptor's source_kind branch and return the raw response.

    Composite branches additionally bind the
    :data:`~meho_backplane.operations._audit.parent_audit_id_var`
    contextvar so the recursive dispatch attaches audit-tree linkage --
    T7 (#398) will promote the linkage to a real column on
    ``audit_log``; T5 lets it ride on the payload.
    """
    if descriptor.source_kind == "ingested":
        assert connector_instance is not None  # resolver miss handled by caller
        return await dispatch_ingested(
            connector=connector_instance,
            descriptor=descriptor,
            operator=operator,
            target=target,
            params=params,
        )
    if descriptor.source_kind == "typed":
        handler = import_handler(descriptor.handler_ref or "")
        handler = _maybe_bind_method(handler, connector_instance)
        return await dispatch_typed(
            handler=handler,
            operator=operator,
            target=target,
            params=params,
        )
    if descriptor.source_kind == "composite":
        handler = import_handler(descriptor.handler_ref or "")
        handler = _maybe_bind_method(handler, connector_instance)
        # Build the ``dispatch_child`` callable bound to this
        # composite's context. The callable owns the parent_audit_id
        # contextvar binding + the composite-depth guard internally so
        # the dispatcher itself stays unaware of recursion semantics
        # -- composite handlers see a plain callable, the audit-tree
        # column gets written automatically, and over-depth attempts
        # raise :class:`CompositeRecursionLimitExceeded` *before* a
        # rogue recursive dispatch fires (handled by the surrounding
        # exception branch in :func:`_execute_and_audit`).
        dispatch_child = get_dispatch_child(
            dispatch=dispatch,
            parent_operator=operator,
            parent_target=target,
            parent_audit_id=audit_id,
            parent_op_id=descriptor.op_id,
        )
        return await dispatch_composite(
            handler=handler,
            operator=operator,
            target=target,
            params=params,
            dispatch_child=dispatch_child,
        )
    # The DB CHECK constraint on source_kind prevents this in practice;
    # the explicit raise keeps the dispatcher's error contract honest
    # if a future migration adds a kind without updating this branch.
    raise RuntimeError(f"unknown source_kind: {descriptor.source_kind!r}")


def _elapsed_ms(started: float) -> float:
    """Wall-clock-since-*started* in milliseconds."""
    return (time.monotonic() - started) * 1000


async def _execute_and_audit(
    *,
    op_id: str,
    descriptor: EndpointDescriptor,
    connector_instance: Connector | None,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    started: float,
) -> OperationResult:
    """Run the source_kind branch, reduce, audit, broadcast, return.

    Wraps the dispatch's success path (steps 6-9) so the main
    :func:`dispatch` body stays a flat sequence of phase calls.
    Failures inside the branch land as ``handler_unreachable`` /
    ``connector_error`` :class:`OperationResult` shapes; the audit row
    still gets written before the return so the operator-visible
    record is consistent with the dispatcher's reply.
    """
    audit_id = uuid.uuid4()
    try:
        raw = await _run_source_kind_branch(
            descriptor=descriptor,
            connector_instance=connector_instance,
            operator=operator,
            target=target,
            params=params,
            audit_id=audit_id,
        )
    except (ImportError, TypeError) as exc:
        # ImportError -- handler_ref couldn't be resolved.
        # TypeError -- resolved symbol wasn't callable.
        duration_ms = _elapsed_ms(started)
        await audit_and_broadcast_safe(
            audit_id=audit_id,
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            params_hash=params_hash,
            result_status="error",
            duration_ms=duration_ms,
        )
        return result_handler_unreachable(op_id, descriptor.handler_ref or "", exc, duration_ms)
    except Exception as exc:
        duration_ms = _elapsed_ms(started)
        await audit_and_broadcast_safe(
            audit_id=audit_id,
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            params_hash=params_hash,
            result_status="error",
            duration_ms=duration_ms,
        )
        return result_connector_error(op_id, exc, duration_ms)

    reduced = await _reduce_or_error(
        op_id=op_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
        params_hash=params_hash,
        audit_id=audit_id,
        raw=raw,
        started=started,
    )
    if isinstance(reduced, OperationResult):
        return reduced
    summary, handle = reduced
    duration_ms = _elapsed_ms(started)
    await audit_and_broadcast_safe(
        audit_id=audit_id,
        operator=operator,
        descriptor=descriptor,
        target=target,
        params=params,
        params_hash=params_hash,
        result_status="ok",
        duration_ms=duration_ms,
    )
    return wrap_ok_result(op_id, summary, duration_ms, handle)


async def _reduce_or_error(
    *,
    op_id: str,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    audit_id: uuid.UUID,
    raw: Any,
    started: float,
) -> tuple[Any, ResultHandle | None] | OperationResult:
    """Run the JSONFlux reducer; return ``(summary, handle)`` or a structured error.

    The dispatcher's module docstring contracts "never raises". v0.2's
    :class:`~meho_backplane.operations.reducer.PassThroughReducer` can't
    raise, but :func:`set_default_reducer` invites swappable real reducers
    (MinIO/S3 I/O, schema validation) that will. Any reducer exception is
    converted to a structured ``connector_error``
    :class:`OperationResult` — same shape the handler-call exception path
    produces — and the audit row + broadcast event still fire so the
    failure is observable.
    """
    reducer_context: dict[str, Any] = {
        "op_id": op_id,
        "operator_sub": operator.sub,
        "audit_id": str(audit_id),
        "source_kind": descriptor.source_kind,
    }
    target_id = getattr(target, "id", None)
    if target_id is not None:
        reducer_context["target_id"] = str(target_id)
    try:
        return await _DEFAULT_REDUCER.reduce(
            raw,
            descriptor.response_schema,
            reducer_context,
        )
    except Exception as exc:
        duration_ms = _elapsed_ms(started)
        await audit_and_broadcast_safe(
            audit_id=audit_id,
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            params_hash=params_hash,
            result_status="error",
            duration_ms=duration_ms,
        )
        return result_connector_error(op_id, exc, duration_ms)


async def dispatch(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
) -> OperationResult:
    """Single entry point for every MEHO operation.

    See the module docstring for the full algorithm + error contract.
    The function never raises; every operator-visible failure mode
    returns a structured :class:`OperationResult`.
    """
    started = time.monotonic()
    params_hash = compute_params_hash(params)
    product, version, impl_id = parse_connector_id(connector_id)

    # --- Step 2: descriptor lookup ----------------------------------------
    descriptor = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
    )
    if descriptor is None:
        known_op_count = await count_known_ops(
            product=product,
            version=version,
            impl_id=impl_id,
        )
        return result_unknown_op(op_id, known_op_count, _elapsed_ms(started))

    # --- Step 3: parameter_schema validation ------------------------------
    validation_errors = validate_params(descriptor.parameter_schema, params)
    if validation_errors:
        return result_invalid_params(op_id, validation_errors, _elapsed_ms(started))

    # --- Step 4: policy gate ---------------------------------------------
    allowed, deny_reason = policy_gate(operator=operator, descriptor=descriptor, target=target)
    if not allowed:
        duration_ms = _elapsed_ms(started)
        await audit_and_broadcast_safe(
            audit_id=uuid.uuid4(),
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            params_hash=params_hash,
            result_status="denied",
            duration_ms=duration_ms,
        )
        return result_denied(op_id, deny_reason or "policy denied", duration_ms)

    # --- Step 5: connector resolution -------------------------------------
    connector_instance, resolution_error = await _resolve_connector_instance(descriptor, target)
    if resolution_error == "no_connector":
        return result_no_connector(op_id, product, version, _elapsed_ms(started))

    # --- Steps 6/7/8/9: branch + reduce + audit + broadcast ---------------
    return await _execute_and_audit(
        op_id=op_id,
        descriptor=descriptor,
        connector_instance=connector_instance,
        operator=operator,
        target=target,
        params=params,
        params_hash=params_hash,
        started=started,
    )
