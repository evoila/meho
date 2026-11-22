# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — core composite-dispatch infrastructure over
# the 600-line ceiling: the resume contextvars, the ``enforce_subop_policy``
# seam that reads them and ``run_governed_subop`` form one tightly-coupled
# mechanism. The record of the steps a run already did lives in
# ``composite_steps``.

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

import enum
import time
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.operations._principal_block_gate import refuse_if_principal_blocked
from meho_backplane.operations.composite_steps import (
    STEP_DONE,
    STEP_DONE_NO_RESULT,
    STEP_RUN_AGAIN,
    StepJournal,
    current_step_journal,
    is_credential_read,
)
from meho_backplane.operations.governed_subops import governed_subops_for
from meho_backplane.settings import get_settings

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:  # pragma: no cover - imports for type checking only
    from collections.abc import Awaitable, Callable

__all__ = [
    "COMPOSITE_DEPTH_TOP_LEVEL",
    "CompositeRecursionLimitExceeded",
    "CompositeResumeState",
    "DispatchChild",
    "SubopStep",
    "approved_run_var",
    "composite_depth_var",
    "composite_dispatch_var",
    "composite_resume_scope_var",
    "composite_resume_state",
    "composite_resume_state_var",
    "composite_resume_var",
    "credential_step_waits_after_approval",
    "enforce_subop_policy",
    "get_dispatch_child",
    "run_governed_subop",
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
#: Correctness contract (#3351 review B1, #3980): this var clears exactly one
#: sub-op. Every other governed sub-op that is not ``destructive`` and not
#: marked ``requires_approval`` goes ahead because a person approved the run
#: (:func:`_covered_by_approved_run`), whoever runs the resume and however
#: the approval was given (web click or command line). A later sub-op that is
#: ``destructive`` or ``requires_approval=True`` waits for its own approval.
#: The steps the earlier runs did are stored on the request
#: (:mod:`~meho_backplane.operations.composite_steps`), so no resume sends
#: one of them again. While this var is still set the run has not reached
#: the approved step, and every step it reaches must be a stored one.
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
#: ``composite_resume_var``. Read by :func:`_covered_by_approved_run` and by
#: :func:`_resume_has_not_reached_approved_step`. ``None`` for every ordinary
#: (non-resume) dispatch.
composite_resume_scope_var: ContextVar[tuple[str, str] | None] = ContextVar(
    "composite_resume_scope",
    default=None,
)


#: ContextVar set to ``True`` while the dispatcher runs an approved call
#: (``dispatch(..., _approved=True)``) (#3980). A person approved this run,
#: so :func:`enforce_subop_policy` lets its internal steps go ahead without a
#: second approval, unless a step is ``destructive`` or marked
#: ``requires_approval`` (:func:`_covered_by_approved_run`). One approval
#: still completes one run, now that a call made with a token (and a
#: ``dangerous`` step in the web interface) waits for approval. ``False`` for
#: every ordinary dispatch.
approved_run_var: ContextVar[bool] = ContextVar("approved_run", default=False)


class CompositeResumeState(NamedTuple):
    """What a parked sub-op saved for the resume of its parent composite (#4217).

    ``op_id`` is the parent composite, ``state`` the JSON the composite
    passed to :func:`enforce_subop_policy` as ``resume_state``, and
    ``after_main_effect`` whether the parked step runs after the
    composite's main change already exists.
    """

    op_id: str
    state: dict[str, Any]
    after_main_effect: bool


#: The :class:`CompositeResumeState` of the approved sub-op, for the whole
#: re-entry of its parent composite (#4217). Bound next to
#: :data:`composite_resume_var` by
#: :func:`~meho_backplane.operations.approval_queue.composite_resume_binding`
#: from ``ApprovalRequest.resume_parent``. The composite reads its state
#: with :func:`composite_resume_state`, so it can reuse what its first run
#: chose (the unique name a VM was deployed under) or continue at the step
#: that waited instead of running from the start. ``None`` for every
#: ordinary dispatch and for a park that saved no state.
composite_resume_state_var: ContextVar[CompositeResumeState | None] = ContextVar(
    "composite_resume_state",
    default=None,
)


def composite_resume_state(op_id: str) -> dict[str, Any] | None:
    """The state the approved sub-op of composite *op_id* saved, during its resume (#4217).

    ``None`` on an ordinary dispatch, when the park saved no state, or when
    the resumed composite is a different one (a nested composite must not
    read the state of its parent).
    """
    bound = composite_resume_state_var.get()
    if bound is None or bound.op_id != op_id:
        return None
    return bound.state


def _resume_has_not_reached_approved_step() -> bool:
    """``True`` while the run after an approval has not run its approved step.

    Until the approved sub-op clears (which consumes
    :data:`composite_resume_var`), every step is one the earlier run did. It
    comes back from the stored steps
    (:mod:`~meho_backplane.operations.composite_steps`); a governed step that
    is not among them is refused (``composite_resume_repeats_write``).
    """
    return composite_resume_scope_var.get() is not None and composite_resume_var.get() is not None


def _resume_continues_at_approved_step() -> bool:
    """``True`` when the approved step is ``after_main_effect``.

    Such a composite reads its saved state and continues at the approved
    step on its own, so it may make new reads on the way there.
    """
    bound = composite_resume_state_var.get()
    return bound is not None and bound.after_main_effect


class DispatchChild(Protocol):
    """Structural callable contract for the sub-op dispatcher composites receive.

    The dispatcher's :func:`~meho_backplane.operations.dispatcher.dispatch`
    function, when ``descriptor.source_kind == 'composite'``, builds a
    :class:`DispatchChild` via :func:`get_dispatch_child` and passes it
    to the composite handler as the ``dispatch_child`` keyword argument.

    Composite handlers declare the parameter against this Protocol::

        async def gh_repo_setup_composite(
            operator: Operator,
            target: Any,
            params: dict[str, Any],
            dispatch_child: DispatchChild,
        ) -> dict[str, Any]:
            repo = await dispatch_child(
                connector_id="gh-rest-3",
                op_id="GET:/repos/{owner}/{repo}",
                params={"owner": params["owner"], "repo": params["repo"]},
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
        effective_target = parent_target if target is None else target
        journal = current_step_journal()
        entry: dict[str, Any] | None = None
        if journal is not None:
            stop = _replayed_child(
                journal,
                connector_id=connector_id,
                op_id=op_id,
                target=effective_target,
                params=params,
            )
            if stop is not None:
                return stop
            # The dispatcher checks a credential read's policy each time it
            # is sent, and marks it when a person's approval let it go ahead.
            entry = journal.start(
                connector_id, op_id, effective_target, params, may_send_again=True
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
            result = await dispatch(
                operator=parent_operator,
                connector_id=connector_id,
                op_id=op_id,
                target=effective_target,
                params=params,
            )
            if journal is not None and entry is not None:
                journal.finish(
                    entry, result.model_dump(mode="json"), ended_well=_child_ended_well(result)
                )
            return result
        finally:
            if journal is not None:
                journal.sending_again = None
            _composite_op_id_chain_var.reset(chain_token)
            composite_depth_var.reset(depth_token)
            parent_audit_id_var.reset(audit_token)

    return _dispatch_child


def _child_ended_well(result: OperationResult) -> bool:
    """True when a ``dispatch_child`` step ran and its change is known to have happened."""
    return result.status == "ok" and result.extras.get("execution_outcome") != "uncertain"


def _replayed_child(
    journal: StepJournal,
    *,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
) -> OperationResult | None:
    """What a ``dispatch_child`` step returns without running, during a resume.

    A step the earlier run finished returns its stored result. A credential
    write or make that ended well returns ``ok`` with no result
    (``extras["result_kept"]`` is ``False``). A ``run_again`` credential read
    runs again, through the dispatcher and its policy check; the approval of
    the run does not cover it
    (:func:`~meho_backplane.operations.composite_steps.child_read_sent_again`).
    A step the earlier run only started stops the run
    (``composite_resume_step_outcome_unknown``).
    Before the approved step, a step the earlier run did not do is refused
    (``composite_resume_repeats_write``), unless the composite continues at
    an ``after_main_effect`` step on its own. ``None``: run the step.
    """
    from meho_backplane.operations._errors import (
        result_composite_resume_repeats_write,
        result_composite_resume_step_outcome_unknown,
    )

    replayed = journal.take(connector_id, op_id, target, params)
    if replayed is not None:
        status = replayed.get("status")
        if status == STEP_RUN_AGAIN:
            _log.info(
                "composite_step_read_again", composite_op_id=journal.composite_op_id, op_id=op_id
            )
            journal.sending_again = op_id
            return None
        if status in (STEP_DONE, STEP_DONE_NO_RESULT):
            _log.info(
                "composite_step_replayed", composite_op_id=journal.composite_op_id, op_id=op_id
            )
            if status == STEP_DONE_NO_RESULT:
                return OperationResult(
                    status="ok", op_id=op_id, duration_ms=0.0, extras={"result_kept": False}
                )
            return OperationResult.model_validate(replayed.get("result"))
        _log.error(
            "composite_resume_step_outcome_unknown",
            composite_op_id=journal.composite_op_id,
            blocked_op_id=op_id,
        )
        return result_composite_resume_step_outcome_unknown(
            composite_op_id=journal.composite_op_id, step_op_id=op_id, duration_ms=0.0
        )
    if _resume_has_not_reached_approved_step() and not _resume_continues_at_approved_step():
        approved = composite_resume_var.get()
        return result_composite_resume_repeats_write(
            composite_op_id=journal.composite_op_id,
            approved_op_id=approved[0] if approved is not None else "<unknown>",
            blocked_op_id=op_id,
            duration_ms=0.0,
        )
    return None


def _subop_resume_cleared(*, op_id: str, params: dict[str, Any]) -> bool:
    """Return ``True`` when an approval-resume just cleared *this* sub-op (#3351).

    Reads :data:`composite_resume_var`, set only on the ``_approved=True``
    re-dispatch of a parent composite whose governed sub-op a human approved.
    When the current sub-op's op_id and *params* match the approved
    ``(op_id, params_hash)`` (checked with
    :func:`~meho_backplane.operations._validate.params_hash_matches`), the
    gate clears — the composite reproduces the approved step on the
    direct seam — and the var is consumed (set to ``None``) so the same
    sub-op cannot clear twice in one re-entry. Matching on
    the params-hash (not op_id alone) pins the clear to the exact entity
    approved. This clears **exactly one** sub-op. Every other governed sub-op
    that is not ``destructive`` and not ``requires_approval`` goes ahead under
    the approval of the run (:func:`_covered_by_approved_run`). A
    governed sub-op that needs its own approval waits for it; the steps this
    run did are stored with that request, so its resume does not send them
    again (:mod:`~meho_backplane.operations.composite_steps`).
    """
    from meho_backplane.operations._validate import params_hash_matches

    approved = composite_resume_var.get()
    if approved is not None and approved[0] == op_id and params_hash_matches(params, approved[1]):
        composite_resume_var.set(None)
        _log.info("composite_subop_resume_cleared", op_id=op_id)
        return True
    return False


def _covered_by_approved_run(
    *,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
    always_wait: bool = False,
    stored_op_class: str | None = None,
) -> bool:
    """True when a person's approval of this run covers this waiting step (#3980).

    The run is approved when the dispatcher runs it with ``_approved=True``
    (:data:`approved_run_var`) or when it is the re-entry of an approved step
    (:data:`composite_resume_scope_var`). Such an approval covers every other
    internal step, except a ``destructive`` step, a step marked
    ``requires_approval``, a step a classification entry makes always
    wait and a step that reads, writes or makes a credential
    (:func:`~meho_backplane.operations.service_grants.credential_call_waits`):
    those keep their own approval.
    """
    from meho_backplane.operations.service_grants import credential_call_waits

    if safety_level == "destructive" or requires_approval or always_wait:
        return False
    if credential_call_waits(op_id, stored_op_class):
        return False
    return _runs_after_approval()


def _runs_after_approval() -> bool:
    """True while a run goes on after a person's approval (:data:`approved_run_var`, a resume)."""
    return approved_run_var.get() or composite_resume_scope_var.get() is not None


def credential_step_waits_after_approval(op_id: str, stored_op_class: str | None = None) -> bool:
    """True when a step or child call of a run after an approval needs its own approval.

    Such a run is checked with the rights of the person who approved it. A
    step or child call that reads, writes or makes a credential
    (:func:`~meho_backplane.operations.service_grants.credential_call_waits`)
    still waits for its own approval, whatever rights that person has and
    whatever level the step declares. The approved step itself is cleared
    before this check (:func:`_subop_resume_cleared`).
    """
    from meho_backplane.operations.service_grants import credential_call_waits

    return _runs_after_approval() and credential_call_waits(op_id, stored_op_class)


def _resume_parent_for_current_composite(
    resume_state: dict[str, Any] | None,
    *,
    after_main_effect: bool,
) -> dict[str, Any] | None:
    """Build the ``ApprovalRequest.resume_parent`` payload from the composite ctx.

    Reads :data:`composite_dispatch_var` (the parent composite's ``(op_id,
    params)``, bound by
    :func:`~meho_backplane.operations._branches.dispatch_composite`) so a
    parked sub-op records the composite to re-enter on resume (#3351). The
    stored ``params`` are the composite's own dispatch params — the resume
    re-dispatches this parent composite verbatim, ``_approved=True``, with the
    approved sub-op pre-cleared. *resume_state*, when given, is stored under
    ``state``, and ``after_main_effect: true`` marks a step that runs after
    the composite's main change exists (#4217); both come back through
    :data:`composite_resume_state_var` during the re-entry. ``steps`` holds
    the steps this run of the composite went through, with their results
    (:class:`~meho_backplane.operations.composite_steps.StepJournal`), so the
    re-entry does not send them again; the key is left out when there are
    none. ``None`` when no composite is on the stack, so the park keeps the
    unchanged generic re-dispatch of its ``op_id``.
    """
    ctx = composite_dispatch_var.get()
    if ctx is None:
        return None
    parent_op_id, parent_params = ctx
    resume_parent: dict[str, Any] = {"op_id": parent_op_id, "params": parent_params}
    if resume_state is not None:
        resume_parent["state"] = resume_state
    if after_main_effect:
        resume_parent["after_main_effect"] = True
    journal = current_step_journal()
    if journal is not None and journal.composite_op_id == parent_op_id:
        steps = journal.stored()
        if steps:
            resume_parent["steps"] = steps
    return resume_parent


def _unfinished_step_before_wait(op_id: str, duration_ms: float) -> OperationResult | None:
    """Stop a step that would wait when an earlier step of the run has no stored end.

    The run after the approval would stop at that earlier step with
    ``composite_resume_step_outcome_unknown`` (or
    ``composite_resume_read_not_sent`` for a credential read a person
    approved), so MEHO does not ask a person for the approval and stops now
    with that error. ``None``: the step may wait.
    The caller skips this check for an ``after_main_effect`` step: its run
    after the approval continues at that step and does not reach the earlier one.
    """
    ctx = composite_dispatch_var.get()
    journal = current_step_journal()
    if ctx is None or journal is None or journal.composite_op_id != ctx[0]:
        return None
    unfinished = journal.first_unfinished()
    if unfinished is None:
        return None
    return _stopped_at_unfinished(journal, unfinished, duration_ms=duration_ms, waiting_op_id=op_id)


def _stopped_at_unfinished(
    journal: StepJournal,
    step_op_id: str,
    *,
    duration_ms: float = 0.0,
    waiting_op_id: str | None = None,
) -> OperationResult:
    """Stop the run at *step_op_id*, a stored step a resume does not send again.

    A credential read gives ``composite_resume_read_not_sent``: a person
    approved it, so a resume does not send it again. Any other step gives
    ``composite_resume_step_outcome_unknown``: MEHO has no stored end for it.
    """
    from meho_backplane.operations._errors import (
        result_composite_resume_read_not_sent,
        result_composite_resume_step_outcome_unknown,
    )

    if is_credential_read(step_op_id):
        _log.error(
            "composite_resume_read_not_sent",
            composite_op_id=journal.composite_op_id,
            blocked_op_id=step_op_id,
            waiting_op_id=waiting_op_id,
        )
        return result_composite_resume_read_not_sent(
            composite_op_id=journal.composite_op_id,
            step_op_id=step_op_id,
            duration_ms=duration_ms,
            waiting_op_id=waiting_op_id,
        )
    _log.error(
        "composite_resume_step_outcome_unknown",
        composite_op_id=journal.composite_op_id,
        blocked_op_id=step_op_id,
        waiting_op_id=waiting_op_id,
    )
    return result_composite_resume_step_outcome_unknown(
        composite_op_id=journal.composite_op_id,
        step_op_id=step_op_id,
        duration_ms=duration_ms,
        waiting_op_id=waiting_op_id,
    )


def _warn_if_step_not_declared(step_op_id: str) -> None:
    """Log one warning when a composite checks a step its list does not have (#6298).

    The up-front step check of an agent call reads that list
    (:mod:`~meho_backplane.operations.governed_subops`), so a step that is
    not on it is not checked before the call starts. The step gate below
    still checks it; this only makes the gap visible.
    """
    composite_ctx = composite_dispatch_var.get()
    if composite_ctx is None:
        return
    composite_op_id = composite_ctx[0]
    surface = governed_subops_for(composite_op_id)
    if surface is not None and step_op_id in surface.sub_op_ids:
        return
    _log.warning(
        "composite_step_not_declared",
        composite_op_id=composite_op_id,
        step_op_id=step_op_id,
    )


async def enforce_subop_policy(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
    target: Any,
    params: dict[str, Any],
    resume_state: dict[str, Any] | None = None,
    after_main_effect: bool = False,
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
      handler returns it verbatim; the dispatcher then writes the parent
      operation's own ``awaiting_approval`` execution row (#4275) and
      returns the result to the caller, so the write **queues** instead of
      executing.
    * ``deny`` → writes a ``denied`` decision row for the step, linked to
      the parent's audit id, and returns a ``denied``
      :class:`OperationResult`; the write never runs. A handler that returns
      it gets the parent operation's own ``denied`` execution row from the
      dispatcher (#4275).

    The seam ships the mechanism only; it does not execute the sub-op or
    write an execution row for it (the top-level composite op owns the row
    that attributes the writes). The chosen model, the rejected
    "top-level-sufficiency" alternative, and the call-site example are
    documented in ``docs/architecture/operations-substrate.md`` under
    "Preserving write-composite policy on the direct path".

    ``resume_state`` and ``after_main_effect`` decide what an approval of
    this sub-op resumes (#4217). Approving a parked sub-op re-runs its
    parent composite with this one sub-op cleared. ``resume_state`` is JSON
    the composite saves on the approval row; the re-run reads it back with
    :func:`composite_resume_state`. ``after_main_effect=True`` says the
    composite's main change already exists when this sub-op runs (the
    rename or cleanup of a deployed VM), and that the composite uses the
    saved state to continue at this step instead of running from the start.
    Two checks keep that safe:

    * ``after_main_effect=True`` without ``resume_state`` is refused
      (``composite_resume_state_missing``): without state the re-run could
      only start from the beginning.
    * During a resume, any other governed step that is not
      ``after_main_effect``, comes before the approved step and is not a
      stored step of the earlier run is refused
      (``composite_resume_repeats_write``).
    * An ``after_main_effect`` step may wait for approval during a resume
      like any other step. Its own resume continues at that step.

    **Steps that already ran.** The seam records each step it lets go ahead
    in the composite's step record
    (:mod:`~meho_backplane.operations.composite_steps`); a step that waits
    stores that record on its approval request. The seam does not see the
    handler's own call, so the step stays "started": when a resume reaches
    it again, the seam returns ``composite_resume_step_outcome_unknown`` and
    the step is not sent again. A step that must be skipped on a resume, with
    its stored result, runs through :func:`run_governed_subop` instead.

    A credential read on the list of reads that change nothing
    (:mod:`~meho_backplane.operations.composite_steps`) is the exception.
    When its policy let it go ahead with no person, a resume checks its policy again
    (:func:`_read_again_refusal`) and lets the same read (same target and
    params) go ahead again only when the policy still lets it go ahead with
    no person; otherwise the run stops with ``composite_resume_read_not_sent``.
    A read that a person approved is not sent again: the run stops with that
    error.

    **A blocked person.** Before anything else, the seam reads the admin
    block list (:mod:`._principal_block_gate`). When the operator is blocked,
    or the list cannot be read, it returns the refusal and the step is not
    sent, also on a resume.
    """
    blocked = await refuse_if_principal_blocked(op_id, operator.sub, operator.tenant_id)
    if blocked is not None:
        return blocked
    journal = current_step_journal()
    replayed = journal.take(connector_id, op_id, target, params) if journal is not None else None
    if journal is not None and replayed is not None:
        if replayed.get("status") != STEP_RUN_AGAIN:
            return _stopped_at_unfinished(journal, op_id)
        refused = await _read_again_refusal(
            journal,
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            safety_level=safety_level,
            requires_approval=requires_approval,
            target=target,
            params=params,
        )
        if refused is None:
            journal.start(connector_id, op_id, target, params, may_send_again=True)
        return refused
    gated = await _gate_subop(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        safety_level=safety_level,
        requires_approval=requires_approval,
        target=target,
        params=params,
        resume_state=resume_state,
        after_main_effect=after_main_effect,
    )
    if isinstance(gated, OperationResult):
        return gated
    if journal is not None:
        journal.start(
            connector_id, op_id, target, params, may_send_again=gated is _GoAhead.NO_PERSON
        )
    return None


class SubopStep(NamedTuple):
    """What :func:`run_governed_subop` gives back.

    ``stopped`` is the result the composite handler returns as is (the step
    waits for approval, is denied, or the run stops); then ``value`` is
    ``None``. Otherwise ``value`` is what the step's call returned, and
    ``replayed`` says it is the stored result of an earlier run.
    """

    stopped: OperationResult | None
    value: Any = None
    replayed: bool = False


async def run_governed_subop(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
    target: Any,
    params: dict[str, Any],
    call: Callable[[], Awaitable[Any]],
    resume_state: dict[str, Any] | None = None,
    after_main_effect: bool = False,
) -> SubopStep:
    """Check a governed direct sub-op with :func:`enforce_subop_policy`'s gate, then run *call*.

    The same gate as :func:`enforce_subop_policy`, with the step's own call
    inside, so MEHO knows when the step ended and what it returned. *call*
    is awaited only when the gate lets the step go ahead. Its result is
    stored in the composite's step record when it is JSON and holds no
    secret-named value (:mod:`~meho_backplane.operations.composite_steps`).

    During the run after an approval, a step the earlier run finished is
    not checked and not sent again: ``value`` is the stored result and
    ``replayed`` is ``True``. A step that writes or makes a credential and
    ended well gives ``value=None`` with ``replayed=True``: its result is
    never kept, so a handler that needs the made or written value in a later
    step gets ``None`` on a resume and must not depend on it. A credential
    read on the list of reads that change nothing
    (:mod:`~meho_backplane.operations.composite_steps`), whose policy let it
    go ahead with no person, is sent again when its policy, checked again,
    still lets it go ahead with no person (:func:`_read_again_refusal`).
    Otherwise, for every other credential read and for a read that a person
    approved, the run stops with ``composite_resume_read_not_sent`` and the
    read is not sent. A step
    the earlier run started but did not finish, or whose result was not
    kept, stops the run with ``composite_resume_step_outcome_unknown``. An
    exception from *call* passes to the handler, and the step stays
    "started".

    When the operator is blocked, or the block list cannot be read, the step
    stops with the refusal and *call* is not awaited, also on a resume.
    """
    blocked = await refuse_if_principal_blocked(op_id, operator.sub, operator.tenant_id)
    if blocked is not None:
        return SubopStep(stopped=blocked)
    journal = current_step_journal()
    replayed = journal.take(connector_id, op_id, target, params) if journal is not None else None
    if journal is not None and replayed is not None:
        if replayed.get("status") != STEP_RUN_AGAIN:
            return _replayed_subop(journal, op_id, replayed)
        refused = await _read_again_refusal(
            journal,
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            safety_level=safety_level,
            requires_approval=requires_approval,
            target=target,
            params=params,
        )
        if refused is not None:
            return SubopStep(stopped=refused)
        may_send_again = True
    else:
        gated = await _gate_subop(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            safety_level=safety_level,
            requires_approval=requires_approval,
            target=target,
            params=params,
            resume_state=resume_state,
            after_main_effect=after_main_effect,
        )
        if isinstance(gated, OperationResult):
            return SubopStep(stopped=gated)
        may_send_again = gated is _GoAhead.NO_PERSON
    entry = (
        journal.start(connector_id, op_id, target, params, may_send_again=may_send_again)
        if journal is not None
        else None
    )
    value = await call()
    if journal is not None and entry is not None:
        journal.finish(entry, value)
    return SubopStep(stopped=None, value=value)


def _replayed_subop(journal: StepJournal, op_id: str, replayed: dict[str, Any]) -> SubopStep:
    """What a :func:`run_governed_subop` step with an entry of the earlier run gives back.

    A finished step gives its stored result (``None`` for a credential write
    or make). A step with no stored end, or a credential read a person
    approved, stops the run.
    """
    if replayed.get("status") in (STEP_DONE, STEP_DONE_NO_RESULT):
        _log.info("composite_step_replayed", composite_op_id=journal.composite_op_id, op_id=op_id)
        return SubopStep(stopped=None, value=replayed.get("result"), replayed=True)
    return SubopStep(stopped=_stopped_at_unfinished(journal, op_id))


async def _read_again_refusal(
    journal: StepJournal,
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
    target: Any,
    params: dict[str, Any],
) -> OperationResult | None:
    """Check the policy of a credential read before a resume sends it again.

    The same policy check as a first send (:func:`apply_step_policy` and
    :func:`policy_gate`). ``None`` when the policy lets the read go ahead
    with no person. Any other answer (it must wait, or it is denied) stops
    the run with ``composite_resume_read_not_sent``, and the read is not
    sent. An approval of this run does not cover the read here.
    """
    from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
    from meho_backplane.operations._lookup import parse_connector_id
    from meho_backplane.operations._validate import policy_gate
    from meho_backplane.operations.op_policy import apply_step_policy
    from meho_backplane.policy.stamp import take_rule

    product, version, impl_id = parse_connector_id(connector_id)
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
    await apply_step_policy(tenant_id=operator.tenant_id, step=descriptor)
    verdict, _reason = await policy_gate(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        params=params,
    )
    take_rule()
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        _log.info("composite_step_read_again", composite_op_id=journal.composite_op_id, op_id=op_id)
        return None
    return _stopped_at_unfinished(journal, op_id)


class _GoAhead(enum.Enum):
    """Why :func:`_gate_subop` let a step go ahead."""

    #: The policy let the step go ahead with no person.
    NO_PERSON = "no_person"
    #: A person's approval cleared the step: its own, or the run's.
    PERSON = "person"


# code-quality-allow: the long policy and approval gate of a direct sub-op,
# kept in one piece; the step record lives in ``enforce_subop_policy`` and
# ``run_governed_subop`` above.
async def _gate_subop(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
    target: Any,
    params: dict[str, Any],
    resume_state: dict[str, Any] | None = None,
    after_main_effect: bool = False,
) -> OperationResult | _GoAhead:
    """The policy and approval gate of :func:`enforce_subop_policy`, without the step record.

    Gives back the result that stops the step, or why the step may go ahead.
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
    from meho_backplane.operations._audit import policy_decision_var, policy_rule_var
    from meho_backplane.operations._errors import (
        result_awaiting_approval,
        result_composite_resume_repeats_write,
        result_composite_resume_state_missing,
        result_denied,
    )
    from meho_backplane.operations._lookup import parse_connector_id
    from meho_backplane.operations._park_check import park_check_refusal
    from meho_backplane.operations._validate import (
        compute_audit_params_hash,
        compute_params_hash,
        policy_gate,
    )
    from meho_backplane.operations.approval_queue import (
        create_pending_request,
        record_approval_step_event,
        resume_requester_var,
        rights_change_note,
    )
    from meho_backplane.operations.dispatcher import (
        _audit_denied_and_return,
        _build_park_effect,
        _destructive_binding_refusal,
        _preflight_denied_refusal,
    )
    from meho_backplane.operations.op_policy import apply_step_policy
    from meho_backplane.operations.service_grants import _sandbox_delete_exception
    from meho_backplane.operations.wait_when_set import raise_descriptor_for_params
    from meho_backplane.policy.stamp import take_rule

    started = time.monotonic()
    _warn_if_step_not_declared(op_id)
    product, version, impl_id = parse_connector_id(connector_id)
    # The hash this step's audit rows carry (secret-named values left out).
    # A parked step stores the full approval hash instead (below).
    params_hash = compute_audit_params_hash(params)

    # #4217: a step that continues after the main change must save the state
    # its resume continues from; without it an approval would re-run the
    # composite from the start.
    if after_main_effect and resume_state is None:
        _log.error("composite_resume_state_missing", op_id=op_id)
        return result_composite_resume_state_missing(
            op_id=op_id, duration_ms=(time.monotonic() - started) * 1000.0
        )

    # #3351: on the approval-resume re-dispatch of the parent composite
    # (``_approved=True``), the one sub-op the human approved clears its gate
    # here — reproducing the approved step through the governed path — instead
    # of re-parking forever. Checked before ``policy_gate`` so a service /
    # agent principal whose sub-op parked resumes without a standing grant.
    if _subop_resume_cleared(op_id=op_id, params=params):
        return _GoAhead.PERSON

    # Before the approved step, every step is one the first run did,
    # and the stored ones were replayed above. A governed step before it that
    # is not itself ``after_main_effect`` is refused before it runs, whatever
    # the policy verdict would be.
    if not after_main_effect and _resume_has_not_reached_approved_step():
        composite_ctx = composite_dispatch_var.get()
        composite_op_id = composite_ctx[0] if composite_ctx is not None else "<unknown>"
        approved = composite_resume_var.get()
        approved_op_id = approved[0] if approved is not None else "<unknown>"
        _log.error(
            "composite_resume_repeats_write",
            composite_op_id=composite_op_id,
            approved_op_id=approved_op_id,
            blocked_op_id=op_id,
        )
        return result_composite_resume_repeats_write(
            composite_op_id=composite_op_id,
            approved_op_id=approved_op_id,
            blocked_op_id=op_id,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

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
    # The override rows of the step's operation and a classification entry
    # can raise the step's level or make it wait, the same as for a called
    # operation. Only the step's group key is read from a stored row; the
    # step runs without one.
    await apply_step_policy(tenant_id=operator.tenant_id, step=descriptor)
    # Some params make a step wait for a person, as for a called operation.
    raise_descriptor_for_params(descriptor, params)

    # #4622: the step's params reach the gate so a delete grant (sandbox
    # mode) can store the step's proposed effect on its grant-use row. No
    # preview hash: a ``destructive`` step stays refused under any grant.
    verdict, reason = await policy_gate(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        params=params,
    )
    step_rule = take_rule()
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        # A step marked for a sandbox delete grant is cleared here only by
        # that grant: no other grant covers it.
        if not credential_step_waits_after_approval(op_id) and (
            not descriptor.credential_call
            or _sandbox_delete_exception(op_id, None, descriptor, params)
        ):
            return _GoAhead.NO_PERSON
        _log.info("composite_credential_step_waits_after_approval", op_id=op_id)
        verdict = PermissionVerdict.NEEDS_APPROVAL
    # #3980: a person approved this run, so a step that would only wait goes
    # ahead. A ``deny`` stays a deny.
    if verdict is PermissionVerdict.NEEDS_APPROVAL and _covered_by_approved_run(
        op_id=op_id,
        safety_level=descriptor.safety_level,
        requires_approval=descriptor.requires_approval,
        always_wait=descriptor.policy_always_wait,
    ):
        _log.info("composite_subop_covered_by_approved_run", op_id=op_id)
        return _GoAhead.PERSON

    # #4646: the rows this gate writes read the verdict off
    # ``policy_decision_var`` (the ``approval.request`` row of a parked step,
    # the ``denied`` row of a refused park). The parent composite's
    # ``dispatch()`` bound its own verdict; bind the step's for the rest of
    # this call and put the parent's back on return, so the parent's own
    # audit row keeps the parent's verdict.
    verdict_token = policy_decision_var.set(verdict.value)
    rule_token = policy_rule_var.set(step_rule)  # #6044, the same scope
    try:
        duration_ms = (time.monotonic() - started) * 1000.0
        if verdict is PermissionVerdict.NEEDS_APPROVAL:
            # The step's own check before the wait for approval, the same as
            # at the dispatcher: a param that must not be stored (a secret
            # given as a plain value) refuses the step before any approval
            # row exists. Audited on the step's path like the refusals below.
            refusal = await park_check_refusal(
                op_id=op_id,
                connector_id=connector_id,
                descriptor=descriptor,
                operator=operator,
                target=target,
                params=params,
                duration_ms=duration_ms,
            )
            if refusal is not None:
                return await _audit_denied_and_return(
                    refusal,
                    operator=operator,
                    descriptor=descriptor,
                    target=target,
                    params=params,
                    params_hash=params_hash,
                    duration_ms=duration_ms,
                )

            # #294 (security review S06): build a real ``proposed_effect`` via the
            # dispatcher's shared helper so this parked sub-op row carries
            # ``safety_level`` + the blast-radius/preview block, exactly like a
            # dispatcher-parked write. Without it ``create_pending_request`` fell
            # back to the identifier-only envelope, which silently disabled the
            # tier-keyed self-approval carve-out (#3290 -- ``_check_self_approval``
            # reads ``proposed_effect['safety_level']``) and blanked the reviewer
            # row's severity / blast-radius (#1855 / #3197). Reusing the dispatcher's
            # ``_build_proposed_effect`` keeps one stamping path rather than
            # diverging; it is fail-soft (returns ``None`` only when connector /
            # preview resolution raises, exactly as the dispatcher park path).
            # When it raises, the row still stores the catalog risk level.
            proposed_effect = await _build_park_effect(
                op_id=op_id,
                connector_id=connector_id,
                descriptor=descriptor,
                operator=operator,
                target=target,
                params=params,
            )

            # #294: a ``destructive``-tier sub-op must fail closed rather than park
            # with only the identifier-only envelope -- the same binding the
            # dispatcher enforces (#3197). The composite seam presents no
            # ``preview_hash``, so this refuses at the binding check (no approval
            # row written) for a destructive sub-op. Defense-in-depth: no shipped
            # caller passes ``destructive`` today, but the seam must never write a
            # weaker row than the dispatcher would for the same tier. #4647: the
            # refusal is audited as a ``denied`` row on the sub-op's path, linked
            # to the parent's audit id, like the preflight refusal below.
            if descriptor.safety_level == "destructive":
                refusal = await _destructive_binding_refusal(
                    op_id=op_id,
                    connector_id=connector_id,
                    operator=operator,
                    target=target,
                    params=params,
                    preview_hash=None,
                    proposed_effect=proposed_effect,
                    duration_ms=duration_ms,
                )
                if refusal is not None:
                    return await _audit_denied_and_return(
                        refusal,
                        operator=operator,
                        descriptor=descriptor,
                        target=target,
                        params=params,
                        params_hash=params_hash,
                        duration_ms=duration_ms,
                    )

            # #3542: same refusal as the dispatcher park — a sub-op whose
            # permission preflight says the write will be denied does not park.
            # The refusal is the step's own decision, so it is recorded here, on
            # the sub-op's path, linked to the parent's audit id, with the same
            # writer as the dispatcher park. The parent composite returns this
            # result as is and gets its own ``denied`` execution row (#4275).
            refusal = _preflight_denied_refusal(
                op_id=op_id, proposed_effect=proposed_effect, duration_ms=duration_ms
            )
            if refusal is not None:
                return await _audit_denied_and_return(
                    refusal,
                    operator=operator,
                    descriptor=descriptor,
                    target=target,
                    params=params,
                    params_hash=params_hash,
                    duration_ms=duration_ms,
                )

            unfinished = (
                None if after_main_effect else _unfinished_step_before_wait(op_id, duration_ms)
            )
            if unfinished is not None:
                return unfinished

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
                    params_hash=compute_params_hash(params),
                    run_id=run_id,
                    # #294: stamp the resolved effect so the row carries its
                    # severity + blast-radius/preview instead of the identifier-only
                    # default the tier-keyed guards silently misread.
                    proposed_effect=proposed_effect,
                    # #3351: record the parent composite so the approval-resume
                    # re-enters it (this sub-op's key + gate params are not a
                    # dispatchable descriptor call), with the state its re-entry
                    # continues from (#4217). ``None`` outside a composite.
                    resume_parent=_resume_parent_for_current_composite(
                        resume_state, after_main_effect=after_main_effect
                    ),
                    # #4217: a step that waits again while an approved request is
                    # re-run belongs to the requester of that request, not to the
                    # reviewer who runs the re-run. ``None`` outside a re-run.
                    requester=resume_requester_var.get(),
                    category=descriptor.policy_category,
                    install_category=descriptor.policy_install_category,
                )
                await session.commit()
            # Record AFTER commit so a step event can never outlive a failed
            # transaction; the helper is fail-open.
            await record_approval_step_event(
                tenant_id=operator.tenant_id,
                request=request,
                decision="pending",
                audit_id=request._audit_id,  # type: ignore[attr-defined]
            )
            return result_awaiting_approval(
                op_id,
                request.id,
                duration_ms,
                audit_id=request.request_audit_id,
                note=await rights_change_note(request),
            )

        # DENY, or any unexpected verdict — fail closed: the sub-op never
        # runs. Only an explicit AUTO_EXECUTE clears the gate. #4275: the step
        # gets its own ``denied`` decision row, linked to the parent's audit
        # id, like a step refused before it could wait above. The parent
        # operation's own ``denied`` execution row is the dispatcher's job.
        return await _audit_denied_and_return(
            result_denied(op_id, reason or "policy denied", duration_ms),
            operator=operator,
            descriptor=descriptor,
            target=target,
            params=params,
            params_hash=params_hash,
            duration_ms=duration_ms,
        )
    finally:
        policy_decision_var.reset(verdict_token)
        policy_rule_var.reset(rule_token)
