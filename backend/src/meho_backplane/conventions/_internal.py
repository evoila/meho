# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure helpers + error vocabulary for :class:`ConventionsService`.

G10.12-T0 (#1894). Splits the side-effect-free pieces of the
conventions service out of :mod:`meho_backplane.conventions.service`
(mirrors the :mod:`meho_backplane.memory._internal` split): the typed
error vocabulary the route / BFF map to their transport, the budget
gate, and the PATCH set-vs-null resolution. None of these touch a DB session -- they operate on
schemas, strings, and ORM rows already in hand -- so isolating them
keeps the service module to its class wiring.
"""

from __future__ import annotations

from meho_backplane.conventions.render import standalone_tokens
from meho_backplane.conventions.schemas import (
    DEFAULT_MAX_PREAMBLE_TOKENS,
    ConventionKind,
    ConventionUpdate,
)
from meho_backplane.db.models import TenantConvention

__all__ = [
    "ConventionConflictError",
    "ConventionNotFoundError",
    "ConventionServiceError",
    "OverBudgetError",
    "enforce_budget",
    "enforce_patch_budget",
    "resolve_patch_fields",
]


class ConventionServiceError(Exception):
    """Base class for the conventions service error vocabulary."""


class ConventionNotFoundError(ConventionServiceError):
    """The ``(tenant_id, slug)`` pair resolves to no row.

    Raised by reads (:meth:`ConventionsService.get_convention`,
    :meth:`ConventionsService.list_history`) and by the update / delete
    writes when the target row is absent or belongs to another tenant.
    The route maps it to 404; collapsing wrong-tenant and wrong-slug
    into one error preserves the tenant-boundary info-leak avoidance
    contract (a cross-tenant probe 404s, never 403s).
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"convention {slug!r} not found")


class ConventionConflictError(ConventionServiceError):
    """A create hit the composite-unique index on ``(tenant_id, slug)``.

    The route maps it to 409. Narrowed to genuine unique-violations by
    :meth:`ConventionsService.create_convention`; other IntegrityError
    shapes propagate so a real corruption surfaces as a 500.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"convention {slug!r} already exists")


class OverBudgetError(ConventionServiceError):
    """An ``operational`` write does not fit the conventions band even alone.

    ``estimated`` is the rendered standalone cost (see
    :func:`~meho_backplane.conventions.render.standalone_tokens`):
    heading, guard prefix, and band delimiters included. The route maps
    it to 422 with a detail naming ``estimated`` vs ``budget``, so a
    CLI / BFF caller can shorten the title or body to fit. ``workflow``
    / ``reference`` kinds never raise this (they are not preamble-bound).
    """

    def __init__(self, estimated: int, budget: int) -> None:
        self.estimated = estimated
        self.budget = budget
        super().__init__(
            "convention exceeds preamble budget when rendered alone "
            f"(estimated={estimated}, budget={budget})"
        )


def enforce_budget(title: str, body: str, kind: ConventionKind) -> None:
    """Raise :class:`OverBudgetError` when an ``operational`` entry cannot fit alone.

    The single-entry write-time gate. It measures the convention the
    way the packer renders it -- the whole band with this entry as its
    only block -- through the shared
    :func:`~meho_backplane.conventions.render.standalone_tokens`
    primitive. A convention that fails this check cannot fit the band
    at any priority, so failing the write loudly is correct (the
    alternative is a silent drop at every future preamble assembly).
    Passing does not promise inclusion: higher-priority entries can
    still use up the shared band. No-op for ``workflow`` /
    ``reference`` (not packed into the preamble).
    """
    if kind is not ConventionKind.OPERATIONAL:
        return
    estimated = standalone_tokens(title, body)
    if estimated > DEFAULT_MAX_PREAMBLE_TOKENS:
        raise OverBudgetError(estimated, DEFAULT_MAX_PREAMBLE_TOKENS)


def resolve_patch_fields(
    body: ConventionUpdate,
    existing: TenantConvention,
) -> tuple[str, str, int]:
    """Pick post-PATCH ``(title, body, priority)`` honouring v2 set-vs-null.

    Pydantic v2's :attr:`BaseModel.model_fields_set` distinguishes
    "field absent from JSON" from "field present with null". For each
    column: take the request body's value iff the field was both
    explicitly set AND non-null; otherwise fall through to the existing
    column.
    """
    explicit = body.model_fields_set
    title = body.title if "title" in explicit and body.title is not None else existing.title
    new_body = body.body if "body" in explicit and body.body is not None else existing.body
    priority = (
        body.priority if "priority" in explicit and body.priority is not None else existing.priority
    )
    return title, new_body, priority


def enforce_patch_budget(
    body: ConventionUpdate,
    new_title: str,
    new_body: str,
    existing: TenantConvention,
) -> None:
    """Run the over-budget gate iff the patch changes rendered text.

    The title and the body are both part of the rendered block, so a
    change to either re-runs the standalone check against the
    post-PATCH values. A priority-only PATCH against an oversize row is
    intentionally not surfaced: the problem was already there at the
    prior write; rejecting an unrelated edit would be confused-deputy
    behaviour. PATCH cannot change kind, so the check validates against
    the existing row's kind. A row carrying a kind outside the closed
    :class:`ConventionKind` vocabulary falls back to ``REFERENCE`` --
    the safe direction; reference-kind never triggers the gate.
    """
    explicit = body.model_fields_set
    title_changed = "title" in explicit and body.title is not None
    body_changed = "body" in explicit and body.body is not None
    if not (title_changed or body_changed):
        return
    try:
        existing_kind = ConventionKind(existing.kind)
    except ValueError:
        existing_kind = ConventionKind.REFERENCE
    enforce_budget(new_title, new_body, existing_kind)
