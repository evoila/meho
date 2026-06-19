# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers + projections for the conventions UI surface.

Initiative #1838 (G10.12 Conventions console), Task #1895 (T1). Pulled
out of :mod:`~meho_backplane.ui.routes.conventions.routes` so the route
handlers stay thin signature wrappers and the projection + render logic
gets a unit-testable seam (no FastAPI :class:`Request` fixture required
for projection logic).

The whole surface is **read-only** in this Task -- list + detail. It
calls the shared :class:`~meho_backplane.conventions.service.ConventionsService`
in-process (threading the route's ``get_session`` dependency), so the
preamble token-budget arithmetic and the ``priority DESC, created_at
ASC`` ordering are identical to the REST ``GET /api/v1/conventions``
surface and to T4's MCP ``initialize`` preamble packer. The UI never
re-derives the budget math.

The headline value is the **always-on budget banner**: it surfaces the
silent preamble overflow drop (an ``operational`` rule the packer
dropped on budget overflow is invisible to agents with no other
signal). The banner reflects the full ``operational`` set regardless
of the active kind tab, mirroring ``budget_status``'s contract.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.conventions.schemas import (
    BudgetStatus,
    Convention,
    ConventionKind,
    ConventionSummary,
)
from meho_backplane.conventions.service import (
    ConventionNotFoundError,
    ConventionsService,
)
from meho_backplane.ui.routes.conventions.operator import ConventionsReadContext
from meho_backplane.ui.routes.memory.render import pygments_css, render_markdown
from meho_backplane.ui.templating import get_templates

__all__ = [
    "KIND_ALL",
    "KIND_TABS",
    "SLUG_MAX_LENGTH",
    "is_htmx_request",
    "render_detail",
    "render_index",
    "resolve_kind_filter",
    "validate_slug",
]

#: Sentinel for the "show every kind" tab. Distinct from the
#: :class:`ConventionKind` enum values so the route's kind param is a
#: closed union of (one-of-three-kinds, ``"all"``); a typo on the query
#: string surfaces as 422 rather than silently collapsing to "all".
KIND_ALL: Final[str] = "all"

#: Ordered (label, value) for the kind tabs in the template. The "All"
#: tab leads so the default landing view shows the full inventory.
KIND_TABS: Final[tuple[tuple[str, str], ...]] = (
    ("All", KIND_ALL),
    ("Operational", ConventionKind.OPERATIONAL.value),
    ("Workflow", ConventionKind.WORKFLOW.value),
    ("Reference", ConventionKind.REFERENCE.value),
)

#: Maximum slug length accepted on the detail path. Mirrors the REST
#: surface's ``_SLUG_MAX_LENGTH`` so a slug that passes the API also
#: passes here.
SLUG_MAX_LENGTH: Final[int] = 128

#: Slug shape enforced at the schema layer
#: (``ConventionCreate.slug`` pattern). The route surface re-validates
#: so a malformed slug arrives as 404 (info-leak avoidance) rather than
#: reaching the service.
_SLUG_PATTERN_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: One shared service instance. The class holds no per-request state
#: (every method takes the ``session``), so a module-level singleton is
#: sound -- mirrors the REST surface's module-level ``_service``.
_service = ConventionsService()


def is_htmx_request(request: Request) -> bool:
    """Return ``True`` when the request was issued by HTMX.

    HTMX 2 sets ``HX-Request: true`` on every fetch its directives
    drive (https://htmx.org/reference/#request_headers).
    """
    return request.headers.get("hx-request", "").lower() == "true"


def resolve_kind_filter(kind: str) -> ConventionKind | None:
    """Parse the tab's ``kind`` query value.

    ``"all"`` -> ``None`` (every kind). A valid :class:`ConventionKind`
    -> that kind. Anything else -> 422 so a typo on the URL doesn't
    silently collapse to "all".
    """
    if kind == KIND_ALL:
        return None
    try:
        return ConventionKind(kind)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"kind must be one of {[k.value for k in ConventionKind]} or 'all'; got {kind!r}"
            ),
        ) from exc


def validate_slug(slug: str) -> None:
    """Translate a malformed slug into 404 at the path-parameter stage.

    Defence-in-depth before the service-layer lookup. Mirrors the REST
    + memory surfaces' posture: malformed slugs surface as 404 (info-
    leak avoidance), not 422.
    """
    if len(slug) > SLUG_MAX_LENGTH or not _SLUG_PATTERN_RE.fullmatch(slug):
        raise HTTPException(status_code=404, detail="convention_not_found")


def _summary_row(entry: ConventionSummary) -> dict[str, object]:
    """Project a :class:`ConventionSummary` into the list-row dict shape."""
    return {
        "slug": entry.slug,
        "title": entry.title,
        "kind": entry.kind,
        "priority": entry.priority,
        "created_by_sub": entry.created_by_sub,
        "updated_at": entry.updated_at,
    }


def _budget_context(budget: BudgetStatus) -> dict[str, object]:
    """Project :class:`BudgetStatus` into the banner's template shape."""
    return {
        "max_tokens": budget.max_tokens,
        "estimated_tokens": budget.estimated_tokens,
        "over_budget": budget.over_budget,
        "dropped_slugs": list(budget.dropped_slugs),
    }


def _detail_context(convention: Convention) -> dict[str, object]:
    """Project a :class:`Convention` into the detail-template shape."""
    return {
        "slug": convention.slug,
        "title": convention.title,
        "kind": convention.kind,
        "priority": convention.priority,
        "body_html": render_markdown(convention.body),
        "created_by_sub": convention.created_by_sub,
        "created_at": convention.created_at,
        "updated_at": convention.updated_at,
    }


def _common_context(read_ctx: ConventionsReadContext) -> dict[str, object]:
    """Build the dict shared across every conventions template render."""
    return {
        "page_title": "Conventions",
        "active_surface": "conventions",
        "operator_sub": read_ctx.operator.sub,
        # UX hint only -- T2's write routes hold the server-side gate.
        "is_tenant_admin": read_ctx.is_tenant_admin,
    }


async def render_index(
    request: Request,
    read_ctx: ConventionsReadContext,
    *,
    session: AsyncSession,
    kind: str = KIND_ALL,
) -> HTMLResponse:
    """Render the list page or the HTMX table fragment.

    The ``kind`` filter narrows the rows only; ``budget_status`` always
    reflects the full ``operational`` set (a ``kind=workflow`` view
    still wants the truthful budget signal) -- the contract
    :meth:`ConventionsService.list_conventions` holds. The banner is
    always-on: it renders the estimated/max token math on every load
    and lists ``dropped_slugs`` in an error style when the preamble
    overflowed.
    """
    kind_filter = resolve_kind_filter(kind)
    entries, budget = await _service.list_conventions(
        session=session,
        operator=read_ctx.operator,
        kind=kind_filter,
    )
    context: dict[str, object] = {
        **_common_context(read_ctx),
        "kind_tabs": KIND_TABS,
        "active_kind": kind,
        "entries": [_summary_row(entry) for entry in entries],
        "entry_count": len(entries),
        "budget": _budget_context(budget),
    }
    template_name = (
        "conventions/_table.html" if is_htmx_request(request) else "conventions/index.html"
    )
    return get_templates().TemplateResponse(request, template_name, context)


async def render_detail(
    request: Request,
    read_ctx: ConventionsReadContext,
    *,
    session: AsyncSession,
    slug: str,
) -> HTMLResponse:
    """Render the detail page or the HTMX body fragment.

    The full ``body`` is rendered server-side through the sanitised
    :func:`render_markdown` (``html=False``) so raw HTML smuggled into a
    convention body renders as escaped text, never live markup. 404 on
    absent OR cross-tenant -- the service's
    :class:`ConventionNotFoundError` collapses both into one status,
    preserving the tenant-boundary info-leak avoidance contract.
    """
    try:
        convention = await _service.get_convention(
            session=session,
            tenant_id=read_ctx.operator.tenant_id,
            slug=slug,
        )
    except ConventionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="convention_not_found") from exc
    context: dict[str, object] = {
        **_common_context(read_ctx),
        "convention": _detail_context(convention),
        "pygments_css": pygments_css(),
    }
    template_name = (
        "conventions/_body_view.html" if is_htmx_request(request) else "conventions/detail.html"
    )
    return get_templates().TemplateResponse(request, template_name, context)
