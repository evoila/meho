# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed PUT-time assignment-authoring validation failures (#2499).

The ``PUT /api/v1/checks/assignment/{runner}`` route validates every
authored item before it stores the document: the target must resolve, and
its op must resolve to an *enabled*, ``safety_level == 'safe'`` endpoint
descriptor the runner can actually execute. A failing item raises one of
these — each carrying a class-level ``error_code`` the route surfaces as a
structured 422 via :mod:`meho_backplane.api.v1._errors`, the same
machine-readable-code discipline as
:class:`~meho_backplane.scheduler.service.EventTriggersNotImplementedError`.

Fail at the *first* offending item and write nothing: an assignment PUT is
a full-document replace, so a partial store would leave the runner with a
half-authored document.
"""

from __future__ import annotations

__all__ = [
    "AssignmentOpNotSafeError",
    "AssignmentOpUnknownError",
    "AssignmentTargetUnknownError",
    "AssignmentValidationError",
]


class AssignmentValidationError(Exception):
    """Base for a rejected authored assignment item.

    Subclasses pin a machine-readable :attr:`error_code`; the route maps
    each to a structured 422 whose ``type`` discriminator is that code.
    """

    #: Machine-readable code a typed client branches on. Overridden per
    #: subclass; the base value is never surfaced (the base is abstract).
    error_code = "assignment_invalid"


class AssignmentTargetUnknownError(AssignmentValidationError):
    """An authored item names a target that does not resolve in the tenant."""

    error_code = "assignment_target_unknown"

    def __init__(self, *, check_ref: str, target_name: str) -> None:
        super().__init__(
            f"assignment item {check_ref!r} names target {target_name!r}, "
            "which does not resolve to a live target in this tenant"
        )


class AssignmentOpUnknownError(AssignmentValidationError):
    """An authored item's op does not resolve to an enabled endpoint descriptor.

    Covers both a target whose connector cannot be resolved (no /
    ambiguous connector) and an op id with no enabled descriptor for the
    resolved ``(product, version, impl_id)`` — the runner would have
    nothing to execute either way.
    """

    error_code = "assignment_op_unknown"

    def __init__(self, *, check_ref: str, op: str, reason: str) -> None:
        super().__init__(
            f"assignment item {check_ref!r} op {op!r} does not resolve to an "
            f"enabled endpoint descriptor: {reason}"
        )


class AssignmentOpNotSafeError(AssignmentValidationError):
    """An authored item's op resolves but is not ``safety_level == 'safe'``.

    v1 of the gateway authorizes read-only workloads only; a
    ``caution``/``dangerous`` op over the runner is a v2 follow-on.
    """

    error_code = "assignment_op_not_safe"

    def __init__(self, *, check_ref: str, op: str, safety_level: str) -> None:
        super().__init__(
            f"assignment item {check_ref!r} op {op!r} has safety_level "
            f"{safety_level!r}; the gateway authorizes only safety_level='safe' "
            "workloads in v1"
        )
