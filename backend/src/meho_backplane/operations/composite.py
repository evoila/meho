# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — core composite-dispatch infrastructure a hair
# over the 600-line ceiling after #3351 review B1 added the fail-closed
# multi-gate resume guard + its ``composite_resume_scope_var`` and the corrected
# resume-contract docstrings. Splitting the contextvars away from the single
# ``enforce_subop_policy`` seam that reads them would scatter one tightly-coupled
# mechanism across modules for a few lines — not warranted.

"""Composite-operation recursion infrastructure for the G0.6 dispatcher.

G0.6-T7 (#398) of Initiative #388. T5 (#396) shipped the dispatcher
plus the ``source_kind='composite'`` branch; this module ships the
runtime contract composite handlers receive when the dispatcher's
``composite`` branch fires:

* :class:`DispatchChild` -- the :class:`typing.Protocol` describing
  the callable a composite handler receives instead of the raw
  :func:`~meho_backplane.operations.dispatcher.dispatch`. Static-type
  checking surface; handlers annotate the parameter against this
  Protocol.
* :func:`get_dispatch_child` -- the factory that builds a real
  ``DispatchChild`` callable bound to a parent operator + target +
  audit_id. The returned callable is what the dispatcher passes to
  the composite handler (``handler(operator, target, params,
  dispatch_child)``).
* :data:`composite_depth_var` -- the contextvar tracking how deep
  the current ``asyncio`` task is into composite recursion.
* :class:`CompositeRecursionLimitExceeded` -- raised when
  ``composite_depth_var`` would exceed
  :attr:`Settings.composite_max_depth`. The dispatcher catches it via
  the generic exception branch and surfaces it as a
  ``connector_error`` :class:`OperationResult` (the composite parent
  fails cleanly; no over-depth audit row is written for the rejected
  sub-call).

Why a Protocol + factory pair
-----------------------------

The factory closes over the parent's ``operator`` / ``target`` /
``audit_id`` so the composite handler doesn't have to re-thread those
values through every sub-call site -- the handler reads as plain
business logic (``await dispatch_child(connector_id, op_id, params)``)
rather than dispatcher-plumbing.

The :class:`DispatchChild` Protocol gives mypy + Pyright a structural
type to bind handler annotations against (composite handlers
declare ``dispatch_child: DispatchChild`` on their signature) without
forcing handlers to import :func:`~meho_backplane.operations.dispatcher.dispatch`
just for typing. Composite handlers ship in
``meho_backplane.connectors.<product>.composites.*`` modules; pinning
those modules to a Protocol rather than to the dispatcher itself keeps
the import graph one-directional (composite handlers depend on the
*contract*, the dispatcher depends on the *handlers*).

Bounded recursion
-----------------

The contextvar :data:`composite_depth_var` carries a non-negative
integer. The dispatcher does not directly increment it; the
``dispatch_child`` callable does, at the boundary between a composite
handler's body and a recursive ``dispatch()`` call. Pre-increment is
checked against :attr:`Settings.composite_max_depth` (default 8 --
see :class:`meho_backplane.settings.Settings`); a would-be-over-depth
call raises :class:`CompositeRecursionLimitExceeded` *before* the
recursive dispatch fires, so no audit row is written for the rejected
sub-op and the parent composite sees a structured exception it can
choose to handle or re-raise. The exception's ``chain`` attribute
carries the ``op_id`` chain that led to the violation, which surfaces
in the parent's ``connector_error`` extras when the parent doesn't
handle the failure.

The contextvar is task-local in :mod:`asyncio` (per the CPython
contextvars contract): two concurrent dispatches see independent depth
counters. The single :func:`asyncio.gather`-fanned-out composite case
is v0.2.next; the v0.2 sequential semantics carry the counter cleanly
because each ``await`` boundary preserves contextvar values.

References
==========

* Parent Initiative -- #388 G0.6 (work item 7).
* Prerequisite -- #396 T5 dispatcher (already exposes the
  ``parent_audit_id_var`` contextvar and the ``composite`` branch
  hook that this module's factory plugs into).
* Audit-tree consumer -- #377 G8.2 audit replay.
* Migration -- ``0006_add_audit_log_parent_audit_id.py`` adds the
  ``audit_log.parent_audit_id`` column the dispatcher writes here.
* Best-practices anchors -- Protocol-based DI for handler-injectable
  callables; :class:`contextvars.ContextVar` for asyncio-safe
  per-task state; bounded recursion to avoid unbounded resource
  consumption from misbehaving handlers.
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.settings import get_settings

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:  # pragma: no cover - imports for type checking only
    from collections.abc import Awaitable, Callable

__all__ = [
    "COMPOSITE_DEPTH_TOP_LEVEL",
    "CompositeRecursionLimitExceeded",
    "DispatchChild",
    "composite_depth_var",
    "composite_dispatch_var",
    "composite_resume_scope_var",
    "composite_resume_var",
    "enforce_subop_policy",
    "get_dispatch_child",
]


#: Sentinel depth for a top-level :func:`dispatch` call -- nothing has
#: incremented the contextvar yet. A composite handler's first
#: ``dispatch_child(...)`` call advances depth to ``1``; the second
#: level (composite-inside-composite, depth-2) advances to ``2``; and
#: so on, until the configured ceiling.
COMPOSITE_DEPTH_TOP_LEVEL: int = 0


#: ContextVar tracking the depth of the current composite-recursion
#: chain. Top-level dispatches see the default (``0``). The
#: :func:`dispatch_child` callable returned by :func:`get_dispatch_child`
#: pre-increments this before each recursive :func:`dispatch` call so
#: a misbehaving handler can't unbounded-recurse: the increment is
#: compared against :attr:`Settings.composite_max_depth`, and an
#: over-depth call raises :class:`CompositeRecursionLimitExceeded`
#: *before* the recursive dispatch fires.
#:
#: Per the asyncio contextvar contract, the value is per-task, not
#: per-process -- two concurrent dispatches see independent counters.
composite_depth_var: ContextVar[int] = ContextVar(
    "composite_depth",
    default=COMPOSITE_DEPTH_TOP_LEVEL,
)


class CompositeRecursionLimitExceeded(RuntimeError):  # noqa: N818 -- name pinned by Task #398 contract
    """Raised when a composite's ``dispatch_child`` would breach the depth cap.

    The dispatcher catches this via its generic exception branch and
    surfaces it as a ``connector_error`` :class:`OperationResult` on
    the *parent* composite -- the over-depth sub-op never runs, no
    audit row is written for it, and the parent composite handler
    sees the exception in the same way it would see any other
    handler-raised :class:`RuntimeError`.

    The exception carries the ``op_id`` chain (parent → child) that
    led to the violation as an attribute, so :func:`repr` / :func:`str`
    output is actionable when the parent composite re-raises it
    verbatim (the most common pattern).
    """

    def __init__(
        self,
        *,
        attempted_depth: int,
        max_depth: int,
        op_id_chain: tuple[str, ...],
    ) -> None:
        self.attempted_depth = attempted_depth
        self.max_depth = max_depth
        self.op_id_chain = op_id_chain
        chain_repr = " -> ".join(op_id_chain) if op_id_chain else "(empty)"
        super().__init__(
            f"composite recursion limit exceeded: attempted depth "
            f"{attempted_depth} > max_depth {max_depth}; "
            f"op_id chain: {chain_repr}"
        )


#: ContextVar accumulating the chain of composite op_ids the current
#: task has descended through. Each ``dispatch_child`` call appends
#: its own op_id before invoking the recursive dispatch and pops it
#: on the way out; the chain is surfaced in
#: :class:`CompositeRecursionLimitExceeded` so operators can see
#: *which* nesting blew the cap.
_composite_op_id_chain_var: ContextVar[tuple[str, ...]] = ContextVar(
    "composite_op_id_chain",
    default=(),
)


#: ContextVar carrying the currently-executing composite's own dispatch
#: identity — ``(op_id, params)`` — for the duration of its handler body.
#: Bound by :func:`~meho_backplane.operations._branches.dispatch_composite`
#: (the same seam that binds ``parent_audit_id_var``). Read by
#: :func:`enforce_subop_policy` so that a governed sub-op parking on the
#: direct seam records its **parent composite** on
#: ``ApprovalRequest.resume_parent`` (#3351): the sub-op's own governance key
#: + identity-only gate params are not a dispatchable descriptor call, so the
#: approval-resume path re-enters the parent composite instead of the raw key.
#: ``None`` when no composite is on the stack (a plain :func:`dispatch`), in
#: which case the park keeps the unchanged generic re-dispatch of its op_id.
composite_dispatch_var: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar(
    "composite_dispatch",
    default=None,
)


#: ContextVar set only on the approval-resume re-dispatch of a parked
#: composite sub-op (#3351): ``(op_id, params_hash)`` of the sub-op the human
#: just approved. Bound by
#: :func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`
#: around the ``_approved=True`` re-dispatch of the parent composite, and read
#: by :func:`enforce_subop_policy` so the one matching sub-op clears its gate
#: (auto-executes) instead of re-parking — reproducing the approved step
#: through the governed path. Consumed (set to ``None``) on the match so a
#: later identical sub-op in the same composite does not clear a second time.
#: ``None`` for every ordinary dispatch.
#:
#: Correctness contract (#3351 review B1): the resume re-enters the parent
#: composite under the **approving reviewer's** identity, and every governed
#: sub-op shipped today is ``dangerous`` + ``requires_approval=False`` — a
#: verdict a USER auto-executes (``policy_gate`` default-allow). So the reviewer
#: runs the approved sub-op **and** every remaining governed sub-op in a single
#: pass; the composite completes in one resume. This var clears exactly one
#: sub-op; the correctness of the rest relies on that auto-execute invariant.
#: A multi-gate re-park — a later governed sub-op that a USER would *not*
#: auto-execute (``destructive`` or ``requires_approval=True``) — is **not
#: supported** by this re-entry mechanism (there is no completed-leg guard, so
#: a second park + second resume would re-run earlier legs). It is detected via
#: :data:`composite_resume_scope_var` and fails closed in
#: :func:`enforce_subop_policy` with ``composite_resume_multi_gate_unsupported``
#: rather than parking.
composite_resume_var: ContextVar[tuple[str, str] | None] = ContextVar(
    "composite_resume",
    default=None,
)


#: ContextVar carrying the approved sub-op's ``(op_id, params_hash)`` for the
#: **whole** approval-resume re-dispatch of a parent composite (#3351 review
#: B1). Bound alongside :data:`composite_resume_var` by
#: :func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`,
#: but — unlike that var — **never consumed**: it stays set for the entire
#: re-entry so :func:`enforce_subop_policy` can tell it is running inside a
#: resume even after the approved sub-op has already cleared and consumed
#: ``composite_resume_var``. Read only by the fail-closed multi-gate guard: if
#: any sub-op *other* than the approved one reaches ``NEEDS_APPROVAL`` during a
#: resume, the composite has more than one governed gate, which this mechanism
#: cannot re-enter safely, so the guard aborts with
#: ``composite_resume_multi_gate_unsupported`` instead of parking a second
#: request. ``None`` for every ordinary (non-resume) dispatch.
composite_resume_scope_var: ContextVar[tuple[str, str] | None] = ContextVar(
    "composite_resume_scope",
    default=None,
)


class DispatchChild(Protocol):
    """Structural callable contract for the sub-op dispatcher composites receive.

    The dispatcher's :func:`~meho_backplane.operations.dispatcher.dispatch`
    function, when ``descriptor.source_kind == 'composite'``, builds a
    :class:`DispatchChild` via :func:`get_dispatch_child` and passes it
    to the composite handler as the ``dispatch_child`` keyword argument.

    Composite handlers declare the parameter against this Protocol::

        async def vmware_vm_create_composite(
            operator: Operator,
            target: Any,
            params: dict[str, Any],
            dispatch_child: DispatchChild,
        ) -> dict[str, Any]:
            folder = await dispatch_child(
                connector_id="vmware-rest-9.0",
                op_id="GET:/api/vcenter/folder",
                params={"filter.names": [params["folder_name"]]},
            )
            ...

    The callable wraps :func:`dispatch`; it inherits the parent
    composite's ``operator`` and (by default) ``target`` so handlers
    don't re-thread them on every sub-call. The ``parent_audit_id``
    contextvar is bound by the callable's body before the recursive
    dispatch fires, so the child's audit row carries the parent's id
    automatically.

    The composite handler can override ``target`` on a per-call basis
    -- e.g. when one composite touches multiple targets (the
    cross-target migration pattern) -- by passing the ``target=``
    keyword. The default is the parent composite's target.

    Protocol vs. typing.Callable
    ----------------------------

    Spelling the contract as a :class:`typing.Protocol` rather than
    a raw ``Callable[..., Awaitable[OperationResult]]`` alias gives
    mypy + Pyright the keyword-argument shape (``connector_id`` /
    ``op_id`` / ``params`` / ``target``) so handler call sites are
    type-checked structurally. A bare ``Callable`` alias would not
    enforce keyword names.
    """

    async def __call__(
        self,
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = ...,
    ) -> OperationResult: ...


def _check_composite_depth(
    *,
    parent_op_id: str,
    child_op_id: str,
) -> int:
    """Read + check the per-task composite depth; return the next depth.

    Pre-increments the would-be depth from
    :data:`composite_depth_var` and compares against
    :attr:`Settings.composite_max_depth` (default 8). Raises
    :class:`CompositeRecursionLimitExceeded` when the next call would
    breach the cap; the exception's ``op_id_chain`` carries the chain
    that led to the violation so operators see which nesting blew
    the cap. Returns the validated next depth so the caller can
    pass it to :func:`composite_depth_var.set`.
    """
    current_depth = composite_depth_var.get()
    attempted_depth = current_depth + 1
    max_depth = get_settings().composite_max_depth
    if attempted_depth > max_depth:
        current_chain = _composite_op_id_chain_var.get()
        raise CompositeRecursionLimitExceeded(
            attempted_depth=attempted_depth,
            max_depth=max_depth,
            op_id_chain=(*current_chain, parent_op_id, child_op_id),
        )
    return attempted_depth


def get_dispatch_child(
    *,
    dispatch: Callable[..., Awaitable[OperationResult]],
    parent_operator: Operator,
    parent_target: Any,
    parent_audit_id: uuid.UUID,
    parent_op_id: str,
) -> DispatchChild:
    """Build a :class:`DispatchChild` callable bound to the parent composite's context.

    Used by the G0.6 dispatcher when ``descriptor.source_kind ==
    'composite'``. The returned callable owns three phases per child
    call: (1) read + check the per-task composite-recursion depth
    against :attr:`Settings.composite_max_depth` via
    :func:`_check_composite_depth` (raise pre-recursion if over-cap);
    (2) bind the audit-tree + depth + op-id-chain contextvars; (3)
    invoke :func:`dispatch` with the parent's operator + the chosen
    target + the child's connector_id/op_id/params; reset the
    contextvars in ``finally`` so siblings see clean state.

    The ``target`` argument on the returned callable defaults to the
    parent composite's target -- composite handlers don't have to
    re-pass it on every sub-call -- but can be overridden per call
    when the composite touches multiple targets (cross-target
    migration pattern).

    Parameters
    ----------
    dispatch:
        The :func:`~meho_backplane.operations.dispatcher.dispatch`
        function. Passed in (rather than imported at module scope)
        to keep this module's import graph one-directional --
        composite handlers depend on this module, the dispatcher
        depends on the composite-handler call site, and a direct
        import would form a cycle.
    parent_operator:
        The composite parent's operator. Inherited by every child
        sub-call so handlers don't re-pass it.
    parent_target:
        The composite parent's target. Default for every child call
        (the per-call ``target=`` override on the returned callable
        wins when supplied).
    parent_audit_id:
        The :class:`uuid.UUID` of the composite parent's audit row.
        Bound on
        :data:`~meho_backplane.operations._audit.parent_audit_id_var`
        for the duration of each child dispatch so the child's audit
        row carries it on its ``parent_audit_id`` column.
    parent_op_id:
        The composite parent's ``op_id``. Used to build the
        ``op_id`` chain that appears in
        :class:`CompositeRecursionLimitExceeded` on a depth violation
        so the operator sees which nesting blew the cap.
    """
    # Local import avoids a hard cycle: the dispatcher imports this
    # module at runtime to build the callable, and this module needs
    # the audit-tree contextvar that lives in ``_audit``. Importing
    # at function scope defers the resolution until the dispatcher
    # actually wires the seam at first composite dispatch.
    from meho_backplane.operations._audit import parent_audit_id_var

    async def _dispatch_child(
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        attempted_depth = _check_composite_depth(
            parent_op_id=parent_op_id,
            child_op_id=op_id,
        )
        # Bind the audit-tree + depth + op-id chain contextvars for
        # the duration of the recursive dispatch. Tokens make resets
        # exception-safe -- ``finally`` restores siblings' clean state.
        audit_token = parent_audit_id_var.set(parent_audit_id)
        depth_token = composite_depth_var.set(attempted_depth)
        chain_token = _composite_op_id_chain_var.set(
            (*_composite_op_id_chain_var.get(), parent_op_id),
        )
        try:
            effective_target = parent_target if target is None else target
            return await dispatch(
                operator=parent_operator,
                connector_id=connector_id,
                op_id=op_id,
                target=effective_target,
                params=params,
            )
        finally:
            _composite_op_id_chain_var.reset(chain_token)
            composite_depth_var.reset(depth_token)
            parent_audit_id_var.reset(audit_token)

    return _dispatch_child


def _subop_resume_cleared(*, op_id: str, params_hash: str) -> bool:
    """Return ``True`` when an approval-resume just cleared *this* sub-op (#3351).

    Reads :data:`composite_resume_var`, set only on the ``_approved=True``
    re-dispatch of a parent composite whose governed sub-op a human approved.
    When the current sub-op's ``(op_id, params_hash)`` matches the approved
    one, the gate clears — the composite reproduces the approved step on the
    direct seam — and the var is consumed (set to ``None``) so the same
    ``(op_id, params_hash)`` cannot clear twice in one re-entry. Matching on
    the params-hash (not op_id alone) pins the clear to the exact entity
    approved. This clears **exactly one** sub-op: the resume then relies on the
    reviewer auto-executing every other governed sub-op in the same pass (all
    ``dangerous`` + ``requires_approval=False``). If any other governed sub-op
    would instead need its own approval, the fail-closed multi-gate guard in
    :func:`enforce_subop_policy` (keyed off the unconsumed
    :data:`composite_resume_scope_var`) aborts the resume rather than re-park it
    (#3351 review B1) — it does **not** re-park the rest.
    """
    approved = composite_resume_var.get()
    if approved is not None and approved == (op_id, params_hash):
        composite_resume_var.set(None)
        _log.info("composite_subop_resume_cleared", op_id=op_id)
        return True
    return False


def _resume_parent_for_current_composite() -> dict[str, Any] | None:
    """Build the ``ApprovalRequest.resume_parent`` payload from the composite ctx.

    Reads :data:`composite_dispatch_var` (the parent composite's ``(op_id,
    params)``, bound by
    :func:`~meho_backplane.operations._branches.dispatch_composite`) so a
    parked sub-op records the composite to re-enter on resume (#3351). The
    stored ``params`` are the composite's own dispatch params — the resume
    re-dispatches this parent composite verbatim, ``_approved=True``, with the
    approved sub-op pre-cleared. ``None`` when no composite is on the stack, so
    the park keeps the unchanged generic re-dispatch of its ``op_id``.
    """
    ctx = composite_dispatch_var.get()
    if ctx is None:
        return None
    parent_op_id, parent_params = ctx
    return {"op_id": parent_op_id, "params": parent_params}


# code-quality-allow: pre-existing 118-line function (predates #3151); this
# change adds the approval-resume sub-op clear + the resume_parent capture
# (#3351) alongside the earlier `connector_id=connector_id` grant pass-through.
async def enforce_subop_policy(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
    target: Any,
    params: dict[str, Any],
) -> OperationResult | None:
    """Re-apply the dispatcher policy/approval gate around a direct-session sub-op.

    The reusable seam that keeps property 3 of #508's four
    ``dispatch_child`` guarantees when a write composite migrates to the
    direct-session path (Task #2254, Initiative #2249). A direct handler
    bypasses :func:`~meho_backplane.operations.dispatcher.dispatch`, so a
    now-internal write sub-op that is itself ``requires_approval`` /
    ``dangerous`` would otherwise execute un-gated. The handler calls
    this **before** each governed direct write sub-call with the sub-op's
    declared policy facts; it re-runs the *same*
    :func:`~meho_backplane.operations._validate.policy_gate` the
    dispatcher runs, against an in-memory
    :class:`~meho_backplane.db.models.EndpointDescriptor` built from those
    facts (never persisted — two-world purity, Goal #2247):

    * ``auto-execute`` → returns ``None``; the handler proceeds with its
      direct ``connector._post_json(...)`` call.
    * ``needs-approval`` → writes a durable
      :class:`~meho_backplane.db.models.ApprovalRequest` for the sub-op
      via :func:`~meho_backplane.operations.approval_queue.create_pending_request`
      and returns an ``awaiting_approval`` :class:`OperationResult`. The
      handler returns it verbatim, and the dispatcher passes a
      handler-returned :class:`OperationResult` straight through, so the
      write **queues** instead of executing.
    * ``deny`` → returns a ``denied`` :class:`OperationResult`; the write
      never runs.

    The seam ships the mechanism only; it does not execute the sub-op or
    write its audit row (the top-level composite op owns the row that
    attributes the writes). The chosen model, the rejected
    "top-level-sufficiency" alternative, and the call-site example are
    documented in ``docs/architecture/operations-substrate.md`` under
    "Preserving write-composite policy on the direct path".
    """
    # Lazy imports: this module is imported by the dispatcher, so a
    # top-level import of the policy/approval machinery (which imports
    # back into the operations package) would risk an import cycle.
    # Deferring to call time mirrors the pattern used by
    # ``get_dispatch_child`` above and ``_handle_needs_approval`` in the
    # dispatcher.
    from meho_backplane.agent.invoke import current_agent_run_id_var
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
    from meho_backplane.operations._errors import (
        result_awaiting_approval,
        result_composite_resume_multi_gate_unsupported,
        result_denied,
    )
    from meho_backplane.operations._lookup import parse_connector_id
    from meho_backplane.operations._validate import compute_params_hash, policy_gate
    from meho_backplane.operations.approval_queue import (
        create_pending_request,
        publish_approval_event,
    )

    started = time.monotonic()
    product, version, impl_id = parse_connector_id(connector_id)
    params_hash = compute_params_hash(params)

    # #3351: on the approval-resume re-dispatch of the parent composite
    # (``_approved=True``), the one sub-op the human approved clears its gate
    # here — reproducing the approved step through the governed path — instead
    # of re-parking forever. Checked before ``policy_gate`` so a service /
    # agent principal whose sub-op parked resumes without a standing grant.
    if _subop_resume_cleared(op_id=op_id, params_hash=params_hash):
        return None

    # An in-memory descriptor carrying only the policy-relevant fields.
    # Never added to a session — it exists solely to feed ``policy_gate``
    # the sub-op's declared governance, exactly as a persisted descriptor
    # would for a ``dispatch()`` call.
    descriptor = EndpointDescriptor(
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        source_kind="composite",
        safety_level=safety_level,
        requires_approval=requires_approval,
        parameter_schema={},
    )

    verdict, reason = await policy_gate(
        operator=operator, descriptor=descriptor, target=target, connector_id=connector_id
    )
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        return None

    duration_ms = (time.monotonic() - started) * 1000.0
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        # #3351 review B1 — fail-closed multi-gate resume guard. The approved
        # sub-op already cleared above (``_subop_resume_cleared`` returned), so
        # reaching NEEDS_APPROVAL while :data:`composite_resume_scope_var` is set
        # means a *second* governed sub-op would park during a resume. Parking it
        # would create a second approval whose own resume re-enters the composite
        # from the top and re-runs every earlier governed leg (this mechanism
        # clears one sub-op and has no completed-leg guard), double-executing
        # writes. No shipped composite reaches here — every governed sub-op is
        # ``dangerous`` + ``requires_approval=False``, which the USER reviewer
        # auto-executes in the single resume pass. Refuse instead of parking.
        resume_scope = composite_resume_scope_var.get()
        if resume_scope is not None:
            approved_op_id, _approved_hash = resume_scope
            composite_ctx = composite_dispatch_var.get()
            composite_op_id = composite_ctx[0] if composite_ctx is not None else "<unknown>"
            _log.error(
                "composite_resume_multi_gate_unsupported",
                composite_op_id=composite_op_id,
                approved_op_id=approved_op_id,
                blocked_op_id=op_id,
            )
            return result_composite_resume_multi_gate_unsupported(
                composite_op_id=composite_op_id,
                approved_op_id=approved_op_id,
                blocked_op_id=op_id,
                duration_ms=duration_ms,
            )

        run_id = current_agent_run_id_var.get()
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
                # #3351: record the parent composite so the approval-resume
                # re-enters it (this sub-op's key + gate params are not a
                # dispatchable descriptor call). ``None`` outside a composite.
                resume_parent=_resume_parent_for_current_composite(),
            )
            await session.commit()
        # Publish AFTER commit so a broadcast can never outlive a failed
        # transaction; the helper is fail-open, so a broadcast outage
        # does not block the durable decision.
        await publish_approval_event(
            tenant_id=operator.tenant_id,
            request=request,
            decision="pending",
            principal_sub=operator.sub,
            audit_id=request._audit_id,  # type: ignore[attr-defined]
        )
        return result_awaiting_approval(op_id, request.id, duration_ms)

    # DENY, or any unexpected verdict — fail closed: the sub-op never
    # runs. Only an explicit AUTO_EXECUTE clears the gate.
    return result_denied(op_id, reason or "policy denied", duration_ms)
