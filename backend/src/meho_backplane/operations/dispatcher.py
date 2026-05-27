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
4. Policy gate (G11.2-T3 + T4;
   :func:`~meho_backplane.operations._validate.policy_gate`). **Agent**
   principals resolve a three-state
   :class:`~meho_backplane.db.models.PermissionVerdict` via the
   per-(principal, op, target) permission model; **human / service**
   principals keep the v0.2 contract (default-allow except
   ``requires_approval=True`` -> ``deny``). Branches:
   ``auto-execute`` proceeds; ``needs-approval`` ->
   :func:`~meho_backplane.operations.approval_queue.create_pending_request`
   writes a durable :class:`~meho_backplane.db.models.ApprovalRequest`
   row and returns an ``awaiting_approval`` result (G11.2-T4 #817);
   ``deny`` -> ``denied`` result. Any other verdict fails closed.
5. Resolve the connector class via
   :func:`~meho_backplane.connectors.resolver.resolve_connector` and
   instantiate it (cached at module level). Resolver miss ->
   structured ``no_connector`` error.
6. Branch on ``descriptor.source_kind`` -- ``ingested`` / ``typed`` /
   ``composite``. See :mod:`meho_backplane.operations._branches`.
7. **Connector-boundary redaction** (G11.4-T2 #1071) -- the raw
   response is captured, the
   :func:`~meho_backplane.redaction.middleware.apply_connector_boundary_redaction`
   helper resolves a per-(connector_id, tenant, op)
   :class:`~meho_backplane.redaction.policy.RedactionPolicy` (falling
   through to the conservative default-safe policy when no override
   is registered) and runs the
   :mod:`~meho_backplane.redaction.engine`. The caller / LLM only ever
   sees the redacted view; the raw payload + the engine's manifest
   land on the audit row (migration ``0030``).
8. JSONFlux-wrap the **redacted** response via the :class:`Reducer`
   (production default is
   :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`,
   installed at startup; the import-time default is the
   :class:`~meho_backplane.operations.reducer.PassThroughReducer` shim).
9. Write the audit row synchronously + publish a broadcast event
   (:func:`~meho_backplane.operations._audit.audit_and_broadcast_safe`).
   The audit row carries the raw payload, the redaction manifest, and
   the resolved policy id.
10. Return the :class:`OperationResult`.

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
  ``extras["exception_message"]`` carries the
  :exc:`~meho_backplane.connectors.NoMatchingConnector` text when the
  resolver was the source (G0.14-T1 #1142); pre-#1142 ingested-branch
  misses pass through the bare ``(product, version)`` form.
* ``ambiguous_connector`` -- the resolver matched two or more
  connectors and the tie-break ladder couldn't pick. The
  :exc:`~meho_backplane.connectors.AmbiguousConnectorResolution`
  message naming the candidate set + the remediation step lands in
  ``extras["exception_message"]``. G0.14-T1 (#1142) added this branch
  to both the ``ingested`` and ``typed``/``composite`` source-kind
  paths; pre-#1142 the exception bubbled past the dispatcher as a
  bare 500.
* ``handler_unreachable`` -- ``importlib`` couldn't resolve
  ``handler_ref``, or the resolved symbol is not callable.
* ``denied`` -- the policy gate issued an outright ``deny`` verdict.
* ``awaiting_approval`` -- the policy gate issued a ``needs_approval``
  verdict; a durable :class:`~meho_backplane.db.models.ApprovalRequest`
  row was created. ``extras["approval_request_id"]`` carries the UUID.
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
    OperationResult,
    ResolutionLabel,
    ResultHandle,
    resolve_connector_or_label,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
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
    result_ambiguous_connector,
    result_awaiting_approval,
    result_composite_l2_missing,
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
    CompositeL2DependencyMissing,
    CompositeRecursionLimitExceeded,
    DispatchChild,
    get_dispatch_child,
)
from meho_backplane.operations.reducer import (
    PassThroughReducer,
    Reducer,
)
from meho_backplane.redaction import (
    RedactionMiddlewareResult,
    apply_connector_boundary_redaction,
    manifest_to_audit_payload,
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
) -> tuple[Connector | None, ResolutionLabel | None, str | None]:
    """Resolve a connector instance for *target* per ``descriptor.source_kind``.

    Returns ``(instance, error_reason, exception_message)``:

    * ``(instance, None, None)`` -- resolver picked a class; instance is
      the cached singleton.
    * ``(None, None, None)`` -- no connector needed (typed/composite
      with ``target is None`` -- a module-level handler that doesn't
      consume a target).
    * ``(None, "no_connector", msg)`` -- resolver miss; the caller
      surfaces this as the ``no_connector`` error and lands ``msg``
      (the :exc:`~meho_backplane.connectors.NoMatchingConnector`
      exception text) under ``extras["exception_message"]`` on the
      :class:`OperationResult`.
    * ``(None, "ambiguous_connector", msg)`` -- resolver matched two
      or more candidates and the tie-break ladder couldn't pick; the
      caller surfaces this as the ``ambiguous_connector`` error and
      lands ``msg`` (the
      :exc:`~meho_backplane.connectors.AmbiguousConnectorResolution`
      text — already naming the candidates + the remediation step)
      under ``extras["exception_message"]``.

    G0.14-T1 (#1142) restructured this helper to:

    1. Mirror the ``ingested`` branch's explicit resolver-miss label on
       the ``typed``/``composite`` branch. Pre-#1142 the typed branch
       silently returned ``(None, None)`` on
       :exc:`NoMatchingConnector`, which let unbound-method handlers
       proceed to :func:`_maybe_bind_method` (which then left them
       unbound) and re-surface as the misleading "typed handler
       reached dispatch still unbound" :exc:`RuntimeError` from
       :func:`~meho_backplane.operations._branches.dispatch_typed`.
       The clean ``no_connector`` is the upstream diagnosis.
    2. Catch :exc:`AmbiguousConnectorResolution` on both branches.
       Pre-#1142 the exception propagated past the dispatcher into
       FastAPI, surfacing as a bare HTTP 500 with no JSON body — and
       the message itself is the most diagnostic single string in the
       MEHO surface (it names the target's ``(product, version)``,
       the conflicting candidates, and the remediation step). The
       label routes through
       :func:`~meho_backplane.operations._errors.result_ambiguous_connector`
       so operators see the resolver's diagnostic verbatim.

    Both branches share the
    :func:`~meho_backplane.connectors.resolve_connector_or_label`
    helper so the dispatcher and the ``/api/v1/targets/{name}/probe``
    route reach the same yes/no/ambiguous answer for the same target
    (consumer feedback signal 19, ``claude-rdc-hetzner-dc#697``).
    """
    if descriptor.source_kind == "ingested":
        cls, label, exc_message = resolve_connector_or_label(target)
        if label is not None:
            return None, label, exc_message
        # cls is guaranteed non-None here (label is None ⇔ cls is set).
        assert cls is not None
        return get_or_create_connector_instance(cls), None, None
    if descriptor.source_kind in ("typed", "composite") and target is not None:
        cls, label, exc_message = resolve_connector_or_label(target)
        if label is not None:
            return None, label, exc_message
        assert cls is not None
        return get_or_create_connector_instance(cls), None, None
    # No target → no resolution attempt. Composite/typed module-level
    # handlers that don't bind to a connector instance land here.
    return None, None, None


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
    connector_id: str,
    descriptor: EndpointDescriptor,
    connector_instance: Connector | None,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    started: float,
) -> OperationResult:
    """Run the source_kind branch, redact, reduce, audit, broadcast, return.

    Wraps the dispatch's success path (steps 6-9) so the main
    :func:`dispatch` body stays a flat sequence of phase calls.
    Failures inside the branch land as ``handler_unreachable`` /
    ``composite_l2_missing`` / ``connector_error`` :class:`OperationResult`
    shapes; the audit row still gets written before the return so the
    operator-visible record is consistent with the dispatcher's reply.

    G11.4-T2 (#1071) inserts the connector-boundary redaction
    middleware between the handler's raw return and the JSONFlux
    reducer: the raw payload is captured, the redaction engine
    rewrites secret-shaped string leaves, and the redacted view is
    what the reducer (and therefore the caller / LLM) sees. The
    audit row records both the raw payload (verbatim) and the
    engine's manifest so an auditor can reconstruct the
    pre-redaction view and a CI gate (#1073) can prove the
    redactor stays deterministic across policy revisions.
    """
    audit_id = uuid.uuid4()
    branch_result = await _run_branch_with_error_handling(
        op_id=op_id,
        descriptor=descriptor,
        connector_instance=connector_instance,
        operator=operator,
        target=target,
        params=params,
        params_hash=params_hash,
        audit_id=audit_id,
        started=started,
    )
    if isinstance(branch_result, OperationResult):
        return branch_result
    raw = branch_result

    # Step 7a -- connector-boundary redaction (G11.4-T2 #1071). The raw
    # payload is captured for the audit row before the engine runs;
    # the engine's redacted output is what flows into the JSONFlux
    # reducer and ultimately back to the caller. Errors inside the
    # middleware surface as ``connector_error`` so the dispatcher's
    # never-raises contract is preserved -- a redactor failure must
    # not leak raw payloads through a 500 with no audit record.
    redaction = _apply_redaction_middleware(
        raw=raw,
        connector_id=connector_id,
        operator=operator,
        op_id=op_id,
    )
    if isinstance(redaction, OperationResult):
        await audit_and_broadcast_safe(
            audit_id=audit_id,
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            params_hash=params_hash,
            result_status="error",
            duration_ms=_elapsed_ms(started),
        )
        return redaction

    return await _reduce_and_audit_success(
        op_id=op_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
        params_hash=params_hash,
        audit_id=audit_id,
        redaction=redaction,
        started=started,
    )


async def _reduce_and_audit_success(
    *,
    op_id: str,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    audit_id: uuid.UUID,
    redaction: RedactionMiddlewareResult,
    started: float,
) -> OperationResult:
    """Run the reducer on the redacted payload, write the success-path
    audit row, and wrap the reduced summary into the final
    :class:`OperationResult`. Extracted from :func:`_execute_and_audit`
    so the orchestrator stays under the code-quality function-size cap
    and the redaction/reduce/audit ordering stays the only thing this
    helper expresses."""
    serialised_manifest = manifest_to_audit_payload(redaction.manifest)
    reduced = await _reduce_or_error(
        op_id=op_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
        params_hash=params_hash,
        audit_id=audit_id,
        raw=redaction.redacted,
        started=started,
        raw_payload_for_audit=redaction.raw,
        redaction_manifest_for_audit=serialised_manifest,
        redaction_policy_id=redaction.policy_id,
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
        raw_payload=redaction.raw,
        redaction_manifest=serialised_manifest,
        redaction_policy_id=redaction.policy_id,
        handle_metadata=_handle_metadata_for_audit(handle),
    )
    return wrap_ok_result(op_id, summary, duration_ms, handle)


async def _run_branch_with_error_handling(
    *,
    op_id: str,
    descriptor: EndpointDescriptor,
    connector_instance: Connector | None,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    audit_id: uuid.UUID,
    started: float,
) -> Any | OperationResult:
    """Invoke the source_kind branch; convert handler errors to OperationResults.

    Extracted from :func:`_execute_and_audit` so the success-path code
    is the linear "redact → reduce → audit → return" sequence. The
    handler's :exc:`ImportError` / :exc:`TypeError` map to
    ``handler_unreachable`` (the importlib walk failed or resolved a
    non-callable); every other exception maps to ``connector_error``.
    Both paths write the audit row before returning so the operator-
    visible record is consistent with the structured failure.

    G0.14-T10 (#1151) adds a structured ``composite_l2_missing`` catch
    ahead of the generic ``except Exception`` so the vmware composite
    pre-flight signal (the catalog-command remediation step) survives
    the audit + reduce pipeline rather than collapsing into the
    opaque ``connector_error`` envelope.
    """
    try:
        return await _run_source_kind_branch(
            descriptor=descriptor,
            connector_instance=connector_instance,
            operator=operator,
            target=target,
            params=params,
            audit_id=audit_id,
        )
    except (ImportError, TypeError) as exc:
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
    except CompositeL2DependencyMissing as l2_exc:
        # G0.14-T10 (#1151): pre-flight detected missing L2 sub-ops.
        # Structured ``composite_l2_missing`` per
        # ``docs/codebase/error-message-shape.md`` rather than the
        # generic ``connector_error`` below. The catch sits ahead of
        # the generic ``except Exception`` so the structured shape wins.
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
        return result_composite_l2_missing(
            op_id, l2_exc.missing_op_ids, l2_exc.catalog_command, duration_ms
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


def _apply_redaction_middleware(
    *,
    raw: Any,
    connector_id: str,
    operator: Operator,
    op_id: str,
) -> RedactionMiddlewareResult | OperationResult:
    """Wrap :func:`apply_connector_boundary_redaction` with error capture.

    The middleware is pure-Python regex over a Pydantic-validated
    policy; it can fail only on (a) the lazy default-policy YAML
    load (e.g. a packaging accident drops ``default.yaml``) or
    (b) an unforeseen Python-level exception inside the engine.
    The dispatcher must convert either to a structured
    ``connector_error`` :class:`OperationResult` rather than letting
    the exception bubble -- the never-raises contract is the only
    reason "store raw → redact → reduce" is safe at runtime. If we
    raised instead, the caller would see a 500 with **no audit
    record of the raw response**, defeating the trust-boundary
    discipline this middleware exists to enforce.

    The ``tenant`` label passes through as the operator's tenant id
    in string form (the resolver compares against the policy
    schema's ``Annotated[str | None]`` field). ``None`` tenant id
    (the chassis-era audit shape) flows through as ``None`` so the
    resolver only matches a non-tenant-scoped override.
    """
    tenant = str(operator.tenant_id) if operator.tenant_id is not None else None
    try:
        return apply_connector_boundary_redaction(
            raw,
            connector_id=connector_id,
            tenant=tenant,
            op=op_id,
        )
    except Exception as exc:
        return result_connector_error(op_id, exc, 0.0)


def _handle_metadata_for_audit(handle: ResultHandle | None) -> dict[str, Any] | None:
    """Build the audit-payload hoist dict for a reducer's ``ResultHandle``.

    G0.15-T8 (#1219). Returns the ``handle_id`` (canonical UUID string),
    ``total_rows`` (int), and ``sample_rows_returned`` (int) the audit
    writer hoists into ``audit_log.payload``. ``None`` when the reducer
    did not materialize -- a pass-through reduce leaves the audit row's
    handle keys absent, which is the right signal for downstream
    consumers (audit-replay G8.2, the audit-query API) that "the
    operator/agent saw the full payload inline; no handle was minted".

    Pulled into a dispatcher helper rather than the reducer so the
    reducer stays decoupled from the audit-write contract (the
    :class:`~meho_backplane.operations.reducer.Reducer` Protocol does
    not surface audit metadata, and the dispatcher already owns the
    redact-reduce-audit ordering).
    """
    if handle is None:
        return None
    sample_count = len(handle.sample_rows) if handle.sample_rows is not None else 0
    return {
        "handle_id": str(handle.handle_id),
        "total_rows": handle.total_rows,
        "sample_rows_returned": sample_count,
    }


def _pagination_hint_from_descriptor(descriptor: EndpointDescriptor) -> dict[str, Any] | None:
    """Extract ``pagination_hint`` from a descriptor's ``llm_instructions``.

    G0.15-T8 (#1219). Connectors that ship pagination-aware ops attach
    a :class:`~meho_backplane.connectors.schemas.PaginationHint`-shaped
    dict under ``llm_instructions.pagination_hint``; the reducer reads
    it via the dispatcher-supplied context to build the
    ``fetch_more.native_pagination`` envelope. Returning a plain dict
    (rather than the validated :class:`PaginationHint`) keeps this
    layer free of a Pydantic-import dependency on the connectors
    schema; the reducer validates at consumption time.

    ``None`` outcomes (op didn't register a hint, ``llm_instructions``
    itself is ``None``, or the slot's value is not a dict) all flow
    to the reducer as "no hint" -- the reducer emits the unavailable
    branch with a curated rationale.
    """
    instructions = descriptor.llm_instructions
    if not isinstance(instructions, dict):
        return None
    raw = instructions.get("pagination_hint")
    if isinstance(raw, dict):
        return raw
    return None


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
    raw_payload_for_audit: Any | None = None,
    redaction_manifest_for_audit: list[dict[str, Any]] | None = None,
    redaction_policy_id: str | None = None,
) -> tuple[Any, ResultHandle | None] | OperationResult:
    """Run the JSONFlux reducer; return ``(summary, handle)`` or a structured error.

    The dispatcher's module docstring contracts "never raises". The
    :class:`~meho_backplane.operations.reducer.PassThroughReducer` shim can't
    raise, but the production
    :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    (and other swappable reducers — DuckDB materialization, future MinIO/S3
    I/O, schema validation) can. Any reducer exception is
    converted to a structured ``connector_error``
    :class:`OperationResult` — same shape the handler-call exception path
    produces — and the audit row + broadcast event still fire so the
    failure is observable.

    *raw_payload_for_audit* / *redaction_manifest_for_audit* /
    *redaction_policy_id* carry the connector-boundary redaction
    artefacts through to the error-path audit row (G11.4-T2 #1071):
    even when the reducer fails, the audit trail still records the
    raw payload + manifest from the (successful) redaction step so
    a debugger has the same pre-redaction evidence available as on
    the success path.
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
    # G0.15-T8 (#1219): forward the op's pagination hint (when the
    # connector author registered one via ``llm_instructions``) into
    # the reducer context so :class:`JsonFluxReducer` can emit the
    # ``fetch_more.native_pagination`` envelope verbatim. ``None`` on
    # ops without a hint -- the reducer falls back to
    # ``available=False`` with a curated rationale. Pulled here (vs.
    # in the reducer) so the reducer stays decoupled from
    # :class:`EndpointDescriptor`'s shape.
    pagination_hint = _pagination_hint_from_descriptor(descriptor)
    if pagination_hint is not None:
        reducer_context["pagination_hint"] = pagination_hint
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
            raw_payload=raw_payload_for_audit,
            redaction_manifest=redaction_manifest_for_audit,
            redaction_policy_id=redaction_policy_id,
        )
        return result_connector_error(op_id, exc, duration_ms)


async def _handle_needs_approval(
    *,
    op_id: str,
    connector_id: str,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    duration_ms: float,
) -> OperationResult:
    """Create a durable pending approval row and return an awaiting_approval result.

    G11.2-T4 (#817). Called when :func:`policy_gate` returns
    ``"needs_approval"``. Opens its own DB session (same pattern as
    :func:`audit_and_broadcast_safe`) so the pending row + request audit
    row commit atomically without coupling to any outer transaction.

    On success returns :func:`~meho_backplane.operations._errors.result_awaiting_approval`
    with the pending row's id in ``extras["approval_request_id"]``.
    On unexpected failure, logs at error level and falls back to a
    ``denied`` result so the dispatcher never raises.
    """
    # Thread the current agent run id (if any) from the contextvar so the
    # approval request links back to the paused run.
    from meho_backplane.agent.invoke import current_agent_run_id_var
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations.approval_queue import (
        create_pending_request,
        publish_approval_event,
    )

    run_id = current_agent_run_id_var.get()

    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            request = await create_pending_request(
                session,
                operator=operator,
                connector_id=connector_id,
                op_id=op_id,
                target=target,
                params=params,
                params_hash=params_hash,
                run_id=run_id,
            )
            await session.commit()
        # Publish AFTER commit so a phantom event cannot outlive a failed
        # transaction (the helper is fail-open: a broadcast outage does
        # not block the durable decision).
        await publish_approval_event(
            tenant_id=operator.tenant_id,
            request=request,
            decision="pending",
            principal_sub=operator.sub,
            audit_id=request._audit_id,  # type: ignore[attr-defined]
        )
        return result_awaiting_approval(op_id, request.id, duration_ms)
    except Exception:
        import structlog as _structlog

        _log = _structlog.get_logger(__name__)
        _log.exception(
            "approval_queue_create_failed",
            op_id=op_id,
            operator_sub=operator.sub,
        )
        # Fall back to denied so the caller gets a structured result
        # and the dispatcher's "never raises" contract is preserved.
        return result_denied(
            op_id,
            "requires_approval is True; approval queue unavailable",
            duration_ms,
        )


# code-quality-allow: 8-phase orchestrator -- phases stay linear so the
# 8-numbered-steps contract in the module docstring stays grep-visible;
# splitting would scatter structured-error returns + obscure never-raises.
async def dispatch(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
    _approved: bool = False,
) -> OperationResult:
    """Single entry point for every MEHO operation.

    See the module docstring for the full algorithm + error contract.
    The function never raises; every operator-visible failure mode
    returns a structured :class:`OperationResult`.

    ``_approved`` is an **internal** flag set only by the approval-queue
    resume path (:mod:`meho_backplane.api.v1.approvals`) after a human
    operator has explicitly approved a parked ``needs-approval`` request.
    It skips the policy gate (Step 4) because the approval decision *is*
    the authorization — re-running the gate would re-deny (the reviewer is
    a human, hard-denied on ``requires_approval``) or re-queue (an agent
    re-hits ``needs-approval``), so an approved op would never execute.
    It is not part of the public agent/MCP/CLI surface; the gate is the
    only authorization path for an ordinary dispatch.
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
    # Skipped on the approval-queue resume path (``_approved``): a human
    # operator already approved this exact call, so re-running the gate
    # would only re-deny or re-queue it. The approval is the authorization.
    if not _approved:
        # G11.2-T3: async, three-state verdict (auto-execute / needs-approval
        # / deny). The call site signature is unchanged; the function now
        # awaits a DB read to load the principal's AgentPermission rows.
        verdict, gate_reason = await policy_gate(
            operator=operator, descriptor=descriptor, target=target
        )
        if verdict == PermissionVerdict.DENY:
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
            return result_denied(op_id, gate_reason or "policy denied", duration_ms)
        if verdict == PermissionVerdict.NEEDS_APPROVAL:
            # G11.2-T4 (#817): write a durable ApprovalRequest row (+ its
            # synchronous "request" audit row) and return an
            # awaiting_approval result. Only agent principals reach this
            # branch — the T3 gate hard-denies requires_approval for
            # human/service principals.
            duration_ms = _elapsed_ms(started)
            return await _handle_needs_approval(
                op_id=op_id,
                connector_id=connector_id,
                operator=operator,
                descriptor=descriptor,
                target=target,
                params=params,
                params_hash=params_hash,
                duration_ms=duration_ms,
            )
        if verdict is not PermissionVerdict.AUTO_EXECUTE:
            # Defensive fail-closed: only an explicit AUTO_EXECUTE proceeds
            # to execution. Any unexpected verdict (a future enum member, a
            # bug in the resolver) denies rather than silently executing.
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
            return result_denied(
                op_id,
                gate_reason or f"unexpected policy verdict {verdict!r}; denied",
                duration_ms,
            )

    # --- Step 5: connector resolution -------------------------------------
    connector_instance, resolution_error, exception_message = await _resolve_connector_instance(
        descriptor, target
    )
    if resolution_error == "no_connector":
        return result_no_connector(
            op_id,
            product,
            version,
            _elapsed_ms(started),
            exception_message=exception_message,
        )
    if resolution_error == "ambiguous_connector":
        # The exception's message is the single most diagnostic string
        # the resolver computes (candidate list + remediation step);
        # surface it verbatim under ``extras["exception_message"]``.
        # ``exception_message`` is guaranteed non-None for this label
        # by ``resolve_connector_or_label``'s contract — the empty
        # string fallback is a defensive type-check guard, never hit.
        return result_ambiguous_connector(
            op_id,
            product,
            version,
            exception_message or "",
            _elapsed_ms(started),
        )

    # --- Steps 6/7/8/9: branch + reduce + audit + broadcast ---------------
    return await _execute_and_audit(
        op_id=op_id,
        connector_id=connector_id,
        descriptor=descriptor,
        connector_instance=connector_instance,
        operator=operator,
        target=target,
        params=params,
        params_hash=params_hash,
        started=started,
    )
