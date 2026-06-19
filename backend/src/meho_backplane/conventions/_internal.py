# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure helpers + error vocabulary for :class:`ConventionsService`.

G10.12-T0 (#1894). Splits the side-effect-free pieces of the
conventions service out of :mod:`meho_backplane.conventions.service`
(mirrors the :mod:`meho_backplane.memory._internal` split): the typed
error vocabulary the route / BFF map to their transport, the budget
gate, the conventions-band slicer, and the PATCH set-vs-null
resolution. None of these touch a DB session -- they operate on
schemas, strings, and ORM rows already in hand -- so isolating them
keeps the service module to its class wiring.
"""

from __future__ import annotations

from meho_backplane.conventions.preamble import BLOCK_END as _CONVENTIONS_BLOCK_END
from meho_backplane.conventions.schemas import (
    DEFAULT_MAX_PREAMBLE_TOKENS,
    ConventionKind,
    ConventionUpdate,
    estimate_tokens,
)
from meho_backplane.db.models import TenantConvention

__all__ = [
    "ConventionConflictError",
    "ConventionNotFoundError",
    "ConventionServiceError",
    "OverBudgetError",
    "conventions_text_only",
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
    """An ``operational`` write exceeds the single-convention preamble budget.

    The route maps it to 422 with a detail naming ``estimated`` vs
    ``budget`` -- the same actionable message the inline gate produced,
    so a CLI / BFF caller can rewrite the body to fit. ``workflow`` /
    ``reference`` kinds never raise this (they are not preamble-bound).
    """

    def __init__(self, estimated: int, budget: int) -> None:
        self.estimated = estimated
        self.budget = budget
        super().__init__(
            f"convention body exceeds preamble budget (estimated={estimated}, budget={budget})"
        )


def conventions_text_only(preamble_text: str) -> str:
    """Return the conventions text band from a combined preamble string.

    The assembled preamble may stitch two text bands: tenant
    conventions wrapped in
    ``<<TENANT_CONVENTIONS ... END_TENANT_CONVENTIONS>>`` followed by
    runbook priming, separated by a blank line. ``budget_status``
    reports the conventions-band token count only -- priming is bounded
    by its own implicit cap and is not charged to the conventions
    budget.

    Strategy: slice up to and including the conventions terminator
    (:data:`~meho_backplane.conventions.preamble.BLOCK_END`); whatever
    follows is the priming band (or empty). When the preamble carries
    only priming (no conventions), the terminator is absent and the
    function returns the empty string -- ``estimate_tokens("") == 0``,
    so the budget status correctly reports zero conventions weight.

    The slice relies only on the wrapper-emitted terminator (never
    substituted from user content per the positional-wrapper discipline
    in :mod:`meho_backplane.conventions.preamble`), so a malicious
    convention body containing the literal terminator string cannot
    cause the slice to mis-attribute priming text as conventions text.
    """
    if not preamble_text:
        return ""
    end = preamble_text.find(_CONVENTIONS_BLOCK_END)
    if end == -1:
        return ""
    return preamble_text[: end + len(_CONVENTIONS_BLOCK_END)]


def enforce_budget(body: str, kind: ConventionKind) -> None:
    """Raise :class:`OverBudgetError` when an ``operational`` body overflows.

    The single-entry write-time gate: a convention whose own token
    estimate exceeds :data:`DEFAULT_MAX_PREAMBLE_TOKENS` cannot fit the
    preamble at any priority, so failing the write loudly is the
    correct response (the alternative is silent drop at every future
    preamble assembly). No-op for ``workflow`` / ``reference`` (not
    packed into the preamble).
    """
    if kind is not ConventionKind.OPERATIONAL:
        return
    estimated = estimate_tokens(body)
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
    new_body: str,
    existing: TenantConvention,
) -> None:
    """Run the over-budget gate iff the patch carries a body change.

    Priority-only or title-only PATCHes against an oversize body are
    intentionally not surfaced: the bug was already there at the prior
    write; rejecting on an unrelated edit would be confused-deputy
    behaviour. PATCH cannot change kind, so the check validates against
    the existing row's kind. A row carrying a kind outside the closed
    :class:`ConventionKind` vocabulary falls back to ``REFERENCE`` --
    the safe direction; reference-kind never triggers the gate.
    """
    if "body" not in body.model_fields_set or body.body is None:
        return
    try:
        existing_kind = ConventionKind(existing.kind)
    except ValueError:
        existing_kind = ConventionKind.REFERENCE
    enforce_budget(new_body, existing_kind)
