# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stop a typed vCenter operation early when the target's vCenter is too old (#4316).

Some vCenter calls exist only from a given version on (Supervisor backup and
restore jobs, the 9.1.1 Supervisor paths, vSphere 9 host settings). A typed
operation that needs such a call uses this module, so every operation says
the same thing in the same words.

* :func:`require_vcenter_version` runs at the top of a handler. On a target
  whose detected version is lower than the minimum, it raises
  :exc:`NotImplementedError` before any vendor call. The dispatcher already
  turns that error into ``connector_unsupported`` with the message in
  ``extras.detail`` and writes the audit row (#1627).
* :func:`add_version_problem` and :func:`version_gated_preview` put the same
  message into the approval request as a ``version_problem`` line. A call that
  waits for approval reaches the queue before the handler runs, so the
  approver needs to see the problem there.

A target with no detected version, or with a version that does not parse,
goes on: the vendor's own error is then classified as usual.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from packaging.version import InvalidVersion, Version

from meho_backplane.connectors.resolver import resolve_target_version
from meho_backplane.operations._preview import PreviewBuilder, PreviewContext

__all__ = [
    "add_version_problem",
    "require_vcenter_version",
    "version_gated_preview",
    "version_problem",
]


def version_problem(target: Any, *, op_id: str, minimum: str, feature: str) -> str | None:
    """Return the "too old" message for *target*, or ``None`` when the call may go on.

    The version comes from :func:`~meho_backplane.connectors.resolver.resolve_target_version`
    (the probed version first, then the version the operator set). ``None``
    when the version is unknown, does not parse, or is at least *minimum*.
    *minimum* must be a valid version string; a bad one is a bug in the
    operation and raises :exc:`~packaging.version.InvalidVersion`.
    """
    needed = Version(minimum)
    version = resolve_target_version(target)
    if version is None:
        return None
    try:
        too_old = Version(version) < needed
    except InvalidVersion:
        return None
    if not too_old:
        return None
    return f"{op_id}: {feature} needs vCenter {minimum} or newer; this target is {version}."


def require_vcenter_version(target: Any, *, op_id: str, minimum: str, feature: str) -> None:
    """Raise :exc:`NotImplementedError` when *target* runs a vCenter older than *minimum*.

    Call it first in a typed handler, before any vendor call::

        async def supervisor_backup_list(self, target, params):
            require_vcenter_version(
                target,
                op_id="vmware.supervisor.backup.list",
                minimum="9.0",
                feature="Supervisor backup jobs",
            )
            ...

    On an 8.0.3 target the caller gets ``connector_unsupported`` with
    ``"vmware.supervisor.backup.list: Supervisor backup jobs needs vCenter
    9.0 or newer; this target is 8.0.3."`` in ``extras.detail``.
    """
    problem = version_problem(target, op_id=op_id, minimum=minimum, feature=feature)
    if problem is not None:
        raise NotImplementedError(problem)


def add_version_problem(
    preview: dict[str, Any] | None,
    target: Any,
    *,
    op_id: str,
    minimum: str,
    feature: str,
) -> dict[str, Any] | None:
    """Add a ``version_problem`` line to *preview* when *target* lacks the call.

    For an operation that already has its own preview builder: call this on
    the builder's result. *preview* is returned unchanged when the version is
    fine; a ``None`` preview becomes ``{"version_problem": ...}`` when it is not.
    """
    problem = version_problem(target, op_id=op_id, minimum=minimum, feature=feature)
    if problem is None:
        return preview
    return {**(preview or {}), "version_problem": problem}


def version_gated_preview(
    *, minimum: str, feature: str, echo: Sequence[str] = ()
) -> PreviewBuilder:
    """Build a preview builder for a version-limited change that has no builder yet.

    The preview shows the params named in *echo* (name only params that hold
    no secrets) and, on a target that lacks the call, the ``version_problem``
    line. Register it with
    :func:`~meho_backplane.operations._preview.register_preview_builder`.
    """
    Version(minimum)  # a bad minimum fails when the op registers, not at approval time

    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        shown = {name: ctx.params[name] for name in echo if name in ctx.params}
        return add_version_problem(
            shown or None,
            ctx.target,
            op_id=ctx.descriptor.op_id,
            minimum=minimum,
            feature=feature,
        )

    return _builder
