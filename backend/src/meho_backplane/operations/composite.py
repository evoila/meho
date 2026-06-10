# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

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

import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover - imports for type checking only
    from collections.abc import Awaitable, Callable

__all__ = [
    "COMPOSITE_DEPTH_TOP_LEVEL",
    "CompositeL2DependencyDisabled",
    "CompositeL2DependencyMissing",
    "CompositeRecursionLimitExceeded",
    "DispatchChild",
    "composite_depth_var",
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


class CompositeL2DependencyMissing(RuntimeError):  # noqa: N818 -- parallels CompositeRecursionLimitExceeded
    """Raised when a composite's declared L2 sub-ops are not all registered.

    G0.14-T10 (#1151). The vmware-rest composites dispatch into raw-REST
    primitives (``GET:/vcenter/datastore`` etc.) that ship as ``ingested``
    descriptors -- they are not part of the default catalog and only land
    after an operator runs ``meho connector ingest --catalog
    <product>/<version>``. Until that ingest happens, calling a composite
    that depends on those primitives crashes mid-dispatch with the
    dispatcher's generic ``unknown_op`` error on the sub-op call; the
    composite parent then surfaces that as a ``connector_error`` wrapping
    a ``RuntimeError`` whose text is essentially "composite sub-op
    'GET:/vcenter/datastore' returned status='error': unknown_op:
    GET:/vcenter/datastore" -- correct, but missing the remediation the
    operator needs (which catalog command to run, which doc to read).

    This exception is the structured equivalent. Each composite handler
    (via the
    :mod:`~meho_backplane.connectors.vmware_rest.composites._preflight`
    helper) walks its declared sub-op_ids before any ``dispatch_child``
    call, validates each is registered in ``endpoint_descriptor``, and
    raises this exception listing the missing ops + the catalog command.

    The dispatcher catches this exception specifically (ahead of the
    generic exception branch) and surfaces it as a structured
    ``composite_l2_missing`` :class:`OperationResult` shape per the
    ``docs/codebase/error-message-shape.md`` convention (G0.14-T11
    #1141) -- the error response carries the code, the missing ops,
    and the catalog command operators must run.

    The :func:`__str__` form is operator-readable so an upstream consumer
    that string-matches on the exception text still gets the salient
    diagnostic.

    Attributes
    ----------
    composite_op_id:
        The composite's own op_id (``vmware.composite.datastore.usage``)
        -- the call site the operator dispatched.
    missing_op_ids:
        Tuple of sub-op_ids that are not registered in
        ``endpoint_descriptor`` and would cause an ``unknown_op`` failure
        on dispatch.
    catalog_command:
        The operator-facing CLI command to run (``meho connector ingest
        --catalog vmware/9.0`` etc.). Resolved per-composite from the
        connector's ``(product, version)``.
    """

    def __init__(
        self,
        *,
        composite_op_id: str,
        missing_op_ids: tuple[str, ...],
        catalog_command: str,
    ) -> None:
        self.composite_op_id = composite_op_id
        self.missing_op_ids = missing_op_ids
        self.catalog_command = catalog_command
        missing_repr = ", ".join(missing_op_ids) if missing_op_ids else "(none)"
        super().__init__(
            f"composite_l2_missing: {composite_op_id!r} depends on L2 sub-ops "
            f"not registered in the catalog: [{missing_repr}]. Run "
            f"{catalog_command!r} to ingest them, then retry."
        )


class CompositeL2DependencyDisabled(RuntimeError):  # noqa: N818 -- parallels CompositeL2DependencyMissing
    """Raised when a composite's L2 sub-ops are present in the catalog but **disabled**.

    #1601. The sibling of :class:`CompositeL2DependencyMissing` for the
    *ingested-but-disabled* deploy state. A descriptor row exists in
    ``endpoint_descriptor`` for the sub-op, but ``is_enabled = false`` --
    so :func:`~meho_backplane.operations._lookup.lookup_descriptor`
    (which hard-filters ``is_enabled = TRUE``) returns ``None`` for it
    exactly as it would for an absent op. The pre-flight disambiguates
    the two via the ``is_enabled``-agnostic
    :func:`~meho_backplane.operations._lookup.descriptor_exists_any_state`
    probe: a sub-op that probe finds present is *disabled*, not *missing*.

    The distinction matters because the two states need opposite
    remediations. *Missing* -> ``meho connector ingest --catalog ...``
    (the catalog has not been ingested). *Disabled* -> re-enable the op;
    the catalog ingest has already happened, so steering the operator
    back to ingest is wrong. The remediation this exception carries names
    a **real** verb -- per-op ``meho connector edit-op <connector_id>
    <op_id> --enable`` -- and explicitly warns that connector-level
    ``meho connector enable`` does **not** cascade to spec-ingested ops
    (they land ``group_id = NULL`` and the enable cascade filters on
    ``group_id``), so per-op ``edit-op --enable`` is the reliable path.

    The dispatcher catches this exception specifically (ahead of both the
    generic exception branch and -- order does not matter, the two are
    disjoint -- the :class:`CompositeL2DependencyMissing` branch) and
    surfaces it as a structured ``composite_l2_disabled``
    :class:`OperationResult` per the
    ``docs/codebase/error-message-shape.md`` convention (#1141).

    Attributes
    ----------
    composite_op_id:
        The composite's own op_id (``vmware.composite.datastore.usage``)
        -- the call site the operator dispatched.
    disabled_op_ids:
        Tuple of sub-op_ids that have a descriptor row in
        ``endpoint_descriptor`` whose ``is_enabled = false``.
    connector_id:
        The connector_id the composite dispatches against
        (``"vmware-rest-9.0"``). Surfaced so the remediation can name the
        exact ``meho connector edit-op <connector_id> <op_id> --enable``
        invocation per disabled op.
    """

    def __init__(
        self,
        *,
        composite_op_id: str,
        disabled_op_ids: tuple[str, ...],
        connector_id: str,
    ) -> None:
        self.composite_op_id = composite_op_id
        self.disabled_op_ids = disabled_op_ids
        self.connector_id = connector_id
        disabled_repr = ", ".join(disabled_op_ids) if disabled_op_ids else "(none)"
        super().__init__(
            f"composite_l2_disabled: {composite_op_id!r} depends on L2 sub-ops "
            f"present in the catalog but disabled: [{disabled_repr}]. Re-enable "
            f"them per-op with "
            f"'meho connector edit-op {connector_id} <op_id> --enable', then retry."
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
