# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing dispatcher debt (>1200 lines on
# main before #1601, which adds only an import + a structured-exception catch
# branch); a split into per-phase modules is its own refactor task, out of
# scope for the composite_l2_disabled classification fix.

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
* ``target_required`` -- a typed/composite op whose handler is a
  connector-bound (self-first) method was invoked with ``target=None``.
  The instance the method binds to is reached *through* the target, so a
  ``None`` target is an omitted-argument usage error. Caught at
  connector-resolution time (G0.20-T6 #1142 follow-up, #1506) before the
  handler proceeds unbound and trips ``dispatch_typed``'s self-guard
  :exc:`RuntimeError`. A module-level handler (no ``self``) needs no
  target and dispatches unchanged.
* ``handler_unreachable`` -- ``importlib`` couldn't resolve
  ``handler_ref``, or the resolved symbol is not callable.
* ``denied`` -- the policy gate issued an outright ``deny`` verdict.
* ``awaiting_approval`` -- the policy gate issued a ``needs_approval``
  verdict; a durable :class:`~meho_backplane.db.models.ApprovalRequest`
  row was created. ``extras["approval_request_id"]`` carries the UUID.
* ``connector_unsupported`` -- the connector / handler raised
  :exc:`NotImplementedError`: it *deliberately* does not implement
  what the dispatch requires (an unsupported ``target.auth_model``;
  an unreplaced ingest auto-shim). G0.23-T1 (#1627). The exception
  message is promoted verbatim into the ``error`` string and
  ``extras["detail"]``; ``extras["cause"]`` distinguishes
  ``unsupported_feature`` (fix the target config) from
  ``unreplaced_auto_shim`` (register the per-product subclass).
* ``connector_http_403`` -- the connector raised
  :exc:`httpx.HTTPStatusError` with a ``403`` status: the credential
  reached the upstream and was authenticated but rejected on
  authorization (e.g. a GitHub App with ``issues: read`` but not
  ``issues: write``). G0.24-T4 (#1649), extending #1627's pattern to
  the transport-error sibling. The ``error`` names the likely
  insufficient-permission cause (connector-agnostically -- any upstream
  403); ``extras`` carries ``http_status=403``, the upstream
  ``upstream_message``, and any standard GitHub permission headers
  (``permission_headers``) that were present. The sibling
  ``connector_http_422`` (#1649) covers a ``422`` invalid-payload
  rejection in the same arm.
* ``connector_auth_failed`` -- the connector raised
  :exc:`httpx.HTTPStatusError` with an auth-class status (typed connectors:
  ``401`` plus vRLI's ``440``; a profiled connector: its profile-declared
  ``expiry_statuses``, the single source #1973 shares with the session
  harness): the credential reached the host but was rejected on
  authentication. T5 (#1804), the auth sibling of ``connector_http_403``
  in the same arm. The session connectors already retry once on a 401
  internally, so a 401 here means re-login *also* failed -- the
  credential is missing/invalid/expired in Vault, or the ``auth_model``
  is wrong. The ``error`` names the ``host``, the status, the likely
  session/credential-expiry or misconfigured-``auth_model`` cause, and
  the verify-the-Vault-credential/``auth_model`` remediation; ``extras``
  carries ``http_status`` (the actual auth-class status), ``host``, and
  the upstream ``upstream_message``. Only the auth-class set is siphoned
  here; every other ``HTTPStatusError`` status (404, 429, 5xx) falls
  through to ``connector_error`` (429 rate-limit is a deliberate
  follow-up, not this surface).
* ``connector_tls_verify_failed`` -- the connector raised
  :exc:`httpx.ConnectError` whose ``__cause__`` is an
  :exc:`ssl.SSLCertVerificationError` (with a ``CERTIFICATE_VERIFY_FAILED``
  substring fallback): the socket opened but the host's certificate chain
  is not trusted (a self-signed / internal-CA appliance). Initiative
  #1774 T3 (#1782), extending the #1627/#1649 dispatch structured-cause
  pattern to the connect-error sibling. The ``error`` names the ``host``
  + three remediations (the global ``SSL_CERT_FILE`` / trust-bundle path,
  the per-target ``tls_ca_pin`` CA-pin secure supersession (T5 #1784), and
  the ``verify_tls=false`` audited last resort); ``extras`` carries
  ``host``, the raw SSL string in ``exception_message``, and the three
  ``remediation_*`` clauses. Only TLS-verify failures are siphoned here;
  every other ``ConnectError`` (DNS, connection-refused, timeout) falls
  through to ``connector_error``.
* ``connector_error`` -- the connector / handler raised any other
  exception (any :exc:`httpx.HTTPStatusError` whose status is none of
  403 / 422 / the auth-class set, any non-TLS :exc:`httpx.ConnectError`
  included, and the reducer / redaction middleware raised *any*
  exception -- the ``connector_unsupported`` / ``connector_http_403`` /
  ``connector_http_422`` / ``connector_auth_failed`` /
  ``connector_tls_verify_failed`` classifications apply only to the
  source-kind branch where connector code runs). The raised exception's
  class name lands in
  ``extras["exception_class"]``; the (length-capped) message in
  ``extras["exception_message"]``.

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

import inspect
import ssl
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import (
    OperationResult,
    ResolutionLabel,
    ResultHandle,
    resolve_connector_or_label,
)
from meho_backplane.connectors.base import Connector, shim_kind
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
    is_auth_failed_status,
    result_ambiguous_connector,
    result_awaiting_approval,
    result_composite_l2_disabled,
    result_composite_l2_missing,
    result_connector_auth_failed,
    result_connector_error,
    result_connector_http_403,
    result_connector_http_422,
    result_connector_tls_verify_failed,
    result_connector_unsupported,
    result_denied,
    result_handler_unreachable,
    result_invalid_params,
    result_no_connector,
    result_target_required,
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
from meho_backplane.operations._preview import (
    PreviewContext,
    build_permission_preflight,
    build_proposed_effect,
)
from meho_backplane.operations._validate import (
    compute_params_hash,
    policy_gate,
    validate_params,
)
from meho_backplane.operations.composite import (
    CompositeL2DependencyDisabled,
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


def _handler_requires_target(handler_ref: str) -> bool:
    """True when *handler_ref* names a connector-bound (self-first) handler.

    Keys the no-target guard on **handler shape**, not just
    ``source_kind`` — a typed/composite handler whose first parameter is
    ``self`` is a connector method that only binds to its instance
    *through* a resolved target, so dispatching it with ``target=None``
    is a usage error (G0.20-T6 #1506). A module-level handler (no
    ``self``) genuinely needs no target and must keep dispatching with
    ``connector_instance=None``.

    Mirrors the first-parameter check :func:`dispatch_typed` uses for its
    self-guard so the two stay in agreement on what "unbound" means.
    Resolution failures here are swallowed to ``False`` on purpose: an
    unimportable / non-callable ``handler_ref`` is the
    ``handler_unreachable`` path's concern, reached when the branch
    re-imports the handler for real — this probe must not pre-empt that
    diagnosis with a misleading ``target_required``.
    """
    try:
        handler = import_handler(handler_ref)
    except (ImportError, TypeError):
        return False
    param_names = list(inspect.signature(handler).parameters.keys())
    return bool(param_names) and param_names[0] == "self"


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
    * ``(None, "target_required", None)`` -- typed/composite op invoked
      with ``target is None`` whose handler is a connector-bound method
      (self-first). The instance the method binds to is reached *through*
      the target, so a ``None`` target is an omitted-argument usage
      error; the caller surfaces it as the ``target_required`` error
      (G0.20-T6 #1506) instead of letting the handler proceed unbound
      and trip the self-guard :exc:`RuntimeError` in
      :func:`~meho_backplane.operations._branches.dispatch_typed`. A
      module-level handler (no ``self``) returns ``(None, None, None)``
      below and dispatches with ``connector_instance=None`` unchanged.

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
    # No target → no resolution attempt. A module-level handler (no
    # ``self``) doesn't bind to a connector instance and dispatches with
    # ``connector_instance=None`` unchanged. A connector-bound (self-first)
    # handler, by contrast, reaches its instance *through* the target — so
    # ``target is None`` is an omitted-argument usage error (G0.20-T6
    # #1506). Catch it here as ``target_required`` rather than letting the
    # handler proceed unbound and trip the loud self-guard RuntimeError in
    # ``dispatch_typed`` (which stays a genuine instance-cache-fault signal).
    if descriptor.source_kind in ("typed", "composite") and _handler_requires_target(
        descriptor.handler_ref or ""
    ):
        return None, "target_required", None
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


def _profile_expiry_statuses(connector_instance: Connector | None) -> frozenset[int] | None:
    """The profile-declared session-expiry status set, or ``None`` if typed.

    A profiled connector carries an
    :class:`~meho_backplane.connectors.profile.ExecutionProfile` whose
    ``expiry_statuses`` is the single source #1973 unifies across the
    session-retry harness and the dispatcher's auth-class arm. Returning it
    here lets :func:`~meho_backplane.operations._errors.is_auth_failed_status`
    classify a profiled connector's auth failure against the *profile's*
    set (default ``{401}``; vRLI ``{401, 440}``) rather than the typed-
    connector global.

    Typed (hand-coded) connectors have no profile, so this returns ``None``
    and the caller falls back to the unchanged global
    ``_AUTH_FAILED_STATUSES`` -- the regression guard in this task's AC. The
    ``getattr`` probe is deliberately duck-typed: the profile is bound onto
    the connector instance by T4 (#1970); until then no instance exposes it
    and every connector classifies via the global, so this change is inert
    for shipped connectors and live only once a profile is attached.
    """
    profile = getattr(connector_instance, "profile", None)
    if profile is None:
        return None
    expiry_statuses = getattr(profile, "expiry_statuses", None)
    if not isinstance(expiry_statuses, frozenset):
        return None
    return expiry_statuses


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

    G0.23-T1 (#1627) adds the symmetric ``connector_unsupported``
    catch for :exc:`NotImplementedError` -- the deliberate "this
    connector doesn't do that" signal (unsupported
    ``target.auth_model``; unreplaced ingest auto-shim) whose
    already-descriptive message used to flatten into the same opaque
    ``connector_error`` envelope.

    G0.24-T4 (#1649) adds ``connector_http_403`` / ``connector_http_422``
    catches for an upstream ``403`` / ``422`` :exc:`httpx.HTTPStatusError`
    -- an insufficient-permission rejection (the credential authenticated
    but lacks the op's required scope) and an invalid-payload rejection
    (the upstream parsed the request but rejected its content), whose
    useful upstream body + GitHub permission headers / ``errors[]``
    validation array used to flatten into the same opaque
    ``connector_error``. Scoped to ``403`` + ``422``; every other status
    falls through to the auth-class check below or ``connector_error``.

    T5 (#1804) adds a ``connector_auth_failed`` catch in the same
    ``httpx.HTTPStatusError`` arm for an auth-class status (``401``, plus
    vRLI's ``440``) -- a re-login failure (the session connectors already
    retry once on a 401) whose missing/invalid/expired-credential or
    misconfigured-``auth_model`` cause used to flatten into the opaque
    ``connector_error`` that made the #1798 vRLI dispatch (``connector_error
    (440)``) unactionable. Scoped to the auth-class set; every other status
    (404, 429, 5xx) still falls through to ``connector_error``.

    Initiative #1774 T3 (#1782) adds a ``connector_tls_verify_failed``
    catch for an :exc:`httpx.ConnectError` whose ``__cause__`` is an
    :exc:`ssl.SSLCertVerificationError` (with a ``CERTIFICATE_VERIFY_FAILED``
    substring fallback) -- the self-signed / internal-CA appliance case,
    whose actionable SSL cause used to vanish into the opaque
    ``connector_error: ConnectError``. Narrowed to TLS-verify failures;
    every other ``ConnectError`` (DNS, connection-refused, timeout) falls
    through to ``connector_error`` unchanged.
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
    except CompositeL2DependencyDisabled as l2_disabled_exc:
        # #1601: pre-flight found L2 sub-ops present in the catalog but
        # disabled. Distinct from ``composite_l2_missing`` below -- the
        # catalog is already ingested, so the remediation is to re-enable
        # the op (``edit-op --enable``), not to re-ingest. Structured
        # ``composite_l2_disabled`` per
        # ``docs/codebase/error-message-shape.md``; sits ahead of the
        # generic ``except Exception`` so the structured shape wins. The
        # disabled / missing catches are disjoint (the pre-flight raises
        # at most one), so their order relative to each other is moot.
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
        return result_composite_l2_disabled(
            op_id,
            l2_disabled_exc.disabled_op_ids,
            l2_disabled_exc.connector_id,
            duration_ms,
        )
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
    except NotImplementedError as nie_exc:
        # G0.23-T1 (#1627): a connector raising NotImplementedError is a
        # deliberate "I don't do this" -- VmwareRestConnector.auth_headers
        # on an unsupported target.auth_model, the ingest auto-shim's
        # auth_headers/execute, a vendor connector's unwired session
        # mode. The raise sites already carry actionable messages;
        # flattening them into the generic ``connector_error`` below
        # buried the diagnostic in extras.exception_message (the RDC
        # cycle-8 dead end). NotImplementedError subclasses RuntimeError,
        # not ImportError/TypeError, so this catch is disjoint from the
        # handler_unreachable branch above; it sits ahead of the generic
        # ``except Exception`` so the structured shape wins.
        #
        # The import is call-time on purpose: the dispatcher is imported
        # everywhere, while ``operations.ingest`` drags the whole
        # ingest-pipeline package (anthropic client, api schemas) in via
        # its __init__. Deferring to the error path keeps dispatcher
        # import light; in production the package is already loaded by
        # the REST routes, so this is a dict lookup.
        from meho_backplane.operations.ingest.connector_registration import (
            sibling_handrolled_impl_id,
        )

        # G0.28-T1 (#1967): only a *bare* auto-shim (GenericRestConnector,
        # shim_kind == "bare") is the "unreplaced auto-shim" dead end. A
        # profiled connector (shim_kind == "profiled") that raises
        # NotImplementedError is a dispatchable connector whose profile
        # wiring is incomplete, not a dead shim — it gets the generic
        # unsupported_feature cause.
        is_auto_shim = connector_instance is not None and shim_kind(connector_instance) == "bare"
        cause: Literal["unsupported_feature", "unreplaced_auto_shim"] = (
            "unreplaced_auto_shim" if is_auto_shim else "unsupported_feature"
        )
        # G0.25-T2 (#1753): when a stray auto-shim is what dispatch
        # landed on, check whether a hand-rolled class for the same
        # (product, version) already ships under a different impl_id.
        # If so, the remediation is "re-ingest under that sibling," not
        # "write a subclass" -- one exists. The shim carries its own
        # (product, version, impl_id) as class attrs; exclude its own
        # impl_id so it cannot name itself.
        sibling_impl_id: str | None = None
        if is_auto_shim and connector_instance is not None:
            sibling_impl_id = sibling_handrolled_impl_id(
                product=connector_instance.product,
                version=connector_instance.version,
                exclude_impl_id=connector_instance.impl_id,
            )
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
        return result_connector_unsupported(
            op_id,
            nie_exc,
            cause=cause,
            connector_class=(
                type(connector_instance).__name__ if connector_instance is not None else None
            ),
            duration_ms=duration_ms,
            sibling_impl_id=sibling_impl_id,
        )
    except httpx.HTTPStatusError as http_exc:
        # G0.24-T4 (#1649): an upstream 403 or 422 carries actionable
        # detail the generic ``connector_error`` arm would bury in
        # ``extras.exception_message`` (consumer
        # ``claude-rdc-hetzner-dc#1138``), because the shared
        # ``HttpConnector`` adapter does no error mapping. Two distinct,
        # connector-agnostic causes:
        #
        # * 403 -> insufficient permission: the credential reached the
        #   upstream and was authenticated, but lacks the scope the op
        #   requires (a GitHub App with ``issues: read`` but not
        #   ``issues: write`` on ``POST /repos/.../issues``). GitHub
        #   returns a body message + ``X-Accepted-GitHub-Permissions`` /
        #   ``x-oauth-scopes`` headers.
        # * 422 -> invalid payload: the upstream parsed the request and
        #   rejected its *content* (a malformed/mis-nested body, a missing
        #   field -- the requestBody-mangling bug T5 #1656). GitHub
        #   returns a body message + an ``errors[]`` array naming the
        #   offending fields.
        #
        # T5 (#1804) extends the same arm to auth-class statuses: a 401
        # (and vRLI's 440) carries an actionable auth/session-failure cause
        # the generic ``connector_error`` likewise buried. The hand-coded
        # session connectors already retry once on a 401 internally
        # (``_get_json_with_session_retry``), so a 401 that reaches the
        # dispatcher means re-login *also* failed -- the credential is
        # missing/invalid/expired in Vault, or the ``auth_model`` is wrong.
        # That flattened to ``connector_error (440)`` on the #1798 vRLI
        # dispatch the operator saw as opaque. ``connector_auth_failed``
        # names the host, status, likely cause, and the Vault-credential /
        # ``auth_model`` remediation.
        #
        # Scoped to 403 + 422 + the auth-class set (``401`` / ``440``,
        # :data:`~meho_backplane.operations._errors._AUTH_FAILED_STATUSES`):
        # every other status (404, 429, 5xx) flattens to the unchanged
        # generic ``connector_error`` shape (429 rate-limit is a separate
        # deliberate follow-up, not this surface). The branch is taken
        # *inside* this arm rather than re-``raise``-ing to the
        # ``except Exception`` below -- a re-raise from one except clause is
        # not caught by a sibling clause of the same ``try``, so it would
        # escape the dispatcher's never-raises contract.
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
        status_code = http_exc.response.status_code
        if status_code == 403:
            return result_connector_http_403(op_id, http_exc, duration_ms)
        if status_code == 422:
            return result_connector_http_422(op_id, http_exc, duration_ms)
        if is_auth_failed_status(status_code, _profile_expiry_statuses(connector_instance)):
            return result_connector_auth_failed(op_id, http_exc, target, duration_ms)
        return result_connector_error(op_id, http_exc, duration_ms)
    except httpx.ConnectError as conn_exc:
        # #1782 (Initiative #1774 T3): a ConnectError carries no
        # ``.response``, so it skips the HTTPStatusError arm above and used
        # to flatten into the generic ``connector_error: ConnectError``
        # below -- discarding the SSL cause, so an operator hitting a
        # self-signed / internal-CA appliance saw only
        # ``[SSL: CERTIFICATE_VERIFY_FAILED]`` with no guidance. When the
        # ConnectError is a TLS-verify failure, emit the structured
        # ``connector_tls_verify_failed`` instead (names the host + both
        # remediations per docs/codebase/error-message-shape.md). The arm
        # sits ahead of the generic ``except Exception`` so the structured
        # shape wins; ``httpx.ConnectError`` (TransportError) and the
        # earlier arms' exception types are disjoint, so no prior
        # classification is perturbed.
        #
        # The connectors' shared ``HttpConnector._retryable`` already
        # retried this ConnectError on idempotent verbs, so by here the
        # retries are exhausted -- a TLS-verify failure is deterministic
        # and never resolves on replay.
        #
        # Narrowing: a TLS-verify failure surfaces as a ConnectError whose
        # ``__cause__`` is an ``ssl.SSLCertVerificationError`` (verified
        # against httpx 0.28.1: the transport raises ConnectError ``from``
        # the underlying ssl error). The ``CERTIFICATE_VERIFY_FAILED``
        # substring is a belt-and-suspenders fallback for the case the
        # cause chain is ever empty. A non-SSL ConnectError (DNS failure,
        # connection refused, connect timeout) matches neither and MUST
        # fall through to ``result_connector_error`` -- never mislabelled
        # as a TLS fault.
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
        is_tls_verify_failure = isinstance(conn_exc.__cause__, ssl.SSLCertVerificationError) or (
            "CERTIFICATE_VERIFY_FAILED" in str(conn_exc)
        )
        if is_tls_verify_failure:
            return result_connector_tls_verify_failed(op_id, conn_exc, target, duration_ms)
        return result_connector_error(op_id, conn_exc, duration_ms)
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


def _result_ordering_from_descriptor(descriptor: EndpointDescriptor) -> dict[str, Any] | None:
    """Extract ``result_ordering`` from a descriptor's ``llm_instructions``.

    G0.19-T1 (#1479). Connectors whose op returns a chronologically-ordered
    collection (``k8s.logs`` emits oldest-first log lines) attach
    ``{"sample": "tail"}`` under ``llm_instructions.result_ordering``; the
    reducer reads it via the dispatcher-supplied context and samples the
    *most-recent* rows for the inline preview instead of the oldest. Mirrors
    :func:`_pagination_hint_from_descriptor` exactly -- a plain dict is
    returned (not a validated model) so this layer stays free of a
    connectors-schema import; the reducer interprets it.

    ``None`` outcomes (op didn't register the hint, ``llm_instructions`` is
    ``None``, or the slot's value is not a dict) all flow to the reducer as
    "no ordering hint" -- it keeps the head-first default.
    """
    instructions = descriptor.llm_instructions
    if not isinstance(instructions, dict):
        return None
    raw = instructions.get("result_ordering")
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
    # G0.20-T7 (#1507): carry the operator's tenant id so the reducer can
    # spill the full materialized set into the tenant-scoped
    # :class:`~meho_backplane.connectors.result_handle_store.ResultHandleStore`
    # keyed by ``(tenant_id, handle_id)``. ``None`` only on operators
    # without a tenant (never on a real dispatch); the reducer then skips
    # the spill and the handle's drill-in surface stays unavailable.
    if operator.tenant_id is not None:
        reducer_context["tenant_id"] = str(operator.tenant_id)
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
    # G0.19-T1 (#1479): forward the op's result-ordering hint (when the
    # connector author registered one via ``llm_instructions``) so the
    # reducer samples the *most-recent* rows of a chronologically-ordered
    # collection (``k8s.logs``) instead of the oldest. ``None`` on ops
    # without a hint -- the reducer keeps the head-first default. Pulled
    # here (vs. in the reducer) for the same decoupling reason as the
    # pagination hint: the reducer stays free of :class:`EndpointDescriptor`.
    result_ordering = _result_ordering_from_descriptor(descriptor)
    if result_ordering is not None:
        reducer_context["result_ordering"] = result_ordering
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


def _identifier_default_effect(
    *,
    op_id: str,
    connector_id: str,
    target: Any,
) -> dict[str, Any]:
    """Build the identifier-only ``proposed_effect`` default.

    Mirrors the default
    :func:`~meho_backplane.operations.approval_queue.create_pending_request`
    constructs when no preview is supplied, so a permission-preflight-only
    park — and a failed-preview park (#1628) — keeps the op identity on
    the row (``{op_id, connector_id, target_id}``) and merely appends the
    preflight banner / unavailability marker to it.
    """
    effect: dict[str, Any] = {"op_id": op_id, "connector_id": connector_id}
    raw_tid = getattr(target, "id", None) if target is not None else None
    if isinstance(raw_tid, uuid.UUID):
        effect["target_id"] = str(raw_tid)
    return effect


async def _build_proposed_effect(
    *,
    op_id: str,
    connector_id: str,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the connector + run the op's preview builder and permission preflight.

    G11.7 follow-up (#1437) + G0.20-T4 (#1504). Resolves the connector
    instance the same way the execute path does (so a ``k8s.apply``
    preview hits the same target the real apply would), assembles a
    :class:`PreviewContext`, and runs two independent, opt-in hooks:

    * :func:`build_proposed_effect` -- the side-effect-free *preview* of
      what the write would do. Suppressed for credential-class ops (the
      durable row must not echo secret material), so most Vault/k8s
      secret writes get ``None`` here.
    * :func:`build_permission_preflight` -- the park-time *permission
      check* (``vault.kv.put`` etc. probe ``sys/capabilities-self``).
      Returns only capability names -- no secret value -- so it runs even
      for credential-class ops and is merged under the
      ``"permission_preflight"`` key.

    Always returns a dict on the normal path: the merged envelope when a
    hook produced one, otherwise the identifier-only base shaped exactly
    like the :func:`~meho_backplane.operations.approval_queue.create_pending_request`
    default. Onto that base it stamps the catalog
    ``descriptor.safety_level`` (#1855) so *every* parked op carries its
    severity (``safe`` / ``caution`` / ``dangerous``) — a ``dangerous``
    op and a ``caution`` op are distinguishable on the reviewer-facing
    row even when neither registers a preview builder. The severity is
    read straight off the descriptor, never recomputed.

    When only the preflight fired, its result is merged onto the
    identifier base so the reviewer still sees the denial banner. A
    *failed* preview (the hook's ``preview_unavailable`` marker, #1628)
    is likewise merged onto the identifier base — the reviewer keeps the
    op identity and additionally sees that the blast radius could not be
    resolved, instead of a bare identifier default indistinguishable
    from a small action. The ``op_class`` / ``preview`` / fail-soft-marker
    envelope built by :func:`build_proposed_effect` itself is unchanged;
    ``safety_level`` is layered on here.

    Returns ``None`` only when connector resolution / hook execution
    raises: those faults degrade to "no preview" (the caller stores its
    own identifier-only default) so the park always proceeds.
    """
    try:
        connector_instance, resolution_error, _ = await _resolve_connector_instance(
            descriptor, target
        )
        if resolution_error is not None:
            connector_instance = None
        ctx = PreviewContext(
            descriptor=descriptor,
            connector_instance=connector_instance,
            operator=operator,
            target=target,
            params=params,
            connector_id=connector_id,
        )
        preview = await build_proposed_effect(ctx)
        if preview is not None and preview.get("preview_unavailable") is True:
            # The registered builder *failed* (vs. declined) — keep the
            # identifier fields the default would have carried and ride
            # the marker + reason alongside them (#1628).
            marked = _identifier_default_effect(
                op_id=op_id, connector_id=connector_id, target=target
            )
            marked.update(preview)
            preview = marked
        preflight = await build_permission_preflight(ctx)
        # The preflight fired; attach it to whatever base the preview
        # produced. When there is no preview (the common case: a
        # suppressed credential-class write, or an op with no registered
        # builder), use the same identifier-only shape
        # ``create_pending_request`` would default to so the row still
        # names the op alongside the severity / denial banner.
        if preview is not None:
            base = dict(preview)
        else:
            base = _identifier_default_effect(op_id=op_id, connector_id=connector_id, target=target)
        if preflight is not None:
            base["permission_preflight"] = preflight
        # Promote the catalog severity onto every parked op's envelope
        # (#1855). ``safety_level`` is op-identity metadata read straight
        # off the descriptor -- not recomputed -- so a parked ``dangerous``
        # op (e.g. ``keycloak.realm.create``) and a ``caution`` op (e.g.
        # ``keycloak.user.create``) produce severity-distinguishable
        # approval rows even when neither registers a preview builder.
        # Stamped here (rather than in the per-op preview hook) so it
        # rides the identifier-only default too, keeping the
        # ``op_class`` / ``preview`` / fail-soft-marker envelope built by
        # :func:`build_proposed_effect` itself unchanged.
        base["safety_level"] = descriptor.safety_level
        return base
    except Exception:
        import structlog as _structlog

        _structlog.get_logger(__name__).warning(
            "proposed_effect_resolution_failed",
            op_id=op_id,
            operator_sub=operator.sub,
            exc_info=True,
        )
        return None


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

    # G11.7 follow-up (#1437): give an op that can compute a
    # side-effect-free preview (notably ``k8s.apply``'s server-dry-run) a
    # chance to populate ``proposed_effect`` so the reviewer sees the diff
    # in the approval queue. Opt-in per op + fail-soft: ops without a
    # registered builder (or whose builder declines) yield ``None`` and
    # the queue stores the identifier-only default exactly as before. A
    # builder that *raises* parks with the identifier fields plus an
    # explicit ``preview_unavailable`` marker + reason (#1628), so the
    # reviewer can tell "blast-radius unknown" from a small action.
    #
    # G0.20-T4 (#1504): the same hook also runs the op's park-time
    # permission preflight (the Vault KV-write ops probe
    # ``sys/capabilities-self``) and merges its capability-only,
    # secret-free result under ``proposed_effect["permission_preflight"]``
    # so a write Vault would deny surfaces a "this write will be denied"
    # banner at park time instead of failing only after a four-eyes
    # approval. Fail-soft too: a probe fault degrades to no banner.
    proposed_effect = await _build_proposed_effect(
        op_id=op_id,
        connector_id=connector_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
    )

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
                proposed_effect=proposed_effect,
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
    the authorization — re-running the gate would re-queue the call (a
    human/service principal now routes ``requires_approval`` to
    ``needs-approval`` per G11.7-T1 #1401; an agent re-hits the same
    floor), so the approved op would loop back into the queue and never
    execute. It is not part of the public agent/MCP/CLI surface; the
    gate is the only authorization path for an ordinary dispatch.
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
            # awaiting_approval result. Reached by an agent principal
            # whose verdict floors to needs-approval AND (G11.7-T1 #1401)
            # by a human/service principal hitting a requires_approval op
            # — both park the call durably for an operator to decide.
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
    if resolution_error == "target_required":
        # G0.20-T6 (#1506): a connector-bound (self-first) typed/composite
        # handler invoked with ``target=None``. Clean usage error before
        # the handler proceeds unbound and trips ``dispatch_typed``'s loud
        # self-guard RuntimeError (preserved for genuine instance-cache
        # faults, which only arise when a target *was* supplied).
        return result_target_required(op_id, _elapsed_ms(started))
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
