# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Memory UI scope-promotion modal render + submit helpers.

Initiative #341 (G10.4 Memory UI), Task #878 (T2). Split out of a
hypothetical combined ``create_promote.py`` module so neither the
create nor the promote concern alone exceeds the chassis-wide
~600-line cap. Shared helpers live in
:mod:`~meho_backplane.ui.routes.memory._modal_shared`; the sibling
create module lives in :mod:`~meho_backplane.ui.routes.memory.create`.

Routes served from this module:

* **``GET /ui/memory/<scope>/<slug>/promote``** -- HTMX-loaded
  ``<dialog>`` with the legal target scopes the operator can promote
  to (derived from :data:`PROMOTE_TARGETS_BY_SOURCE`). Terminal source
  scopes (``tenant`` / ``target``) return 400; the detail page does
  not render a Promote button for those scopes either.
* **``POST /ui/memory/<scope>/<slug>/promote``** -- submit handler.
  Calls :meth:`MemoryService.promote` (G5.2 #374) and returns 204
  with ``HX-Redirect: /ui/memory/<target-scope>/<slug>`` so HTMX
  navigates to the new detail page.

Idempotency
-----------

The promote service is idempotent (a re-promotion to the same target
scope returns the existing target row, no insert, no duplicate audit
side-effect). The UI layer trusts that contract -- a double-click on
the Confirm button redirects to the same URL twice; no extra row,
no 409 conflict.

Audit row contract
------------------

Per Initiative #341 / Task #878, the promotion **writes an audit
row** so G8 forensic queries can reconstruct who promoted what when.
The chassis :class:`~meho_backplane.audit.AuditMiddleware` writes
one row per request iff ``operator_sub`` is bound to the structlog
contextvars at the time the audit hook fires. The promote handler
binds ``operator_sub`` + ``tenant_id`` + ``audit_op_id="memory.promote"``
+ ``audit_op_class="write"`` + ``audit_scope`` + ``audit_slug`` +
``audit_promotion_target_scope`` -- same shape the
``/api/v1/memory/{scope}/{slug}/promote`` REST handler uses.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.memory import (
    InvalidPromotionStepError,
    MemoryScope,
    MemoryService,
    PermissionDeniedError,
)
from meho_backplane.memory.schemas import TARGET_SCOPED
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.memory._modal_shared import (
    TARGET_NAME_MAX_LENGTH,
    build_common_template_context,
    scope_label,
    set_csrf_cookie,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["PROMOTE_TARGETS_BY_SOURCE", "promote_entry", "render_promote_modal"]

_log = structlog.get_logger(__name__)


#: Legal source-to-targets map for the promotion modal. Mirrors
#: :data:`meho_backplane.memory.rbac._PROMOTION_LADDER` but expressed
#: as ``source -> [targets]`` so the modal template can iterate the
#: legal next steps for a memory at *source* scope without re-deriving
#: the ladder on the template side. The two ladders the rbac module
#: encodes (``user -> user-tenant -> tenant`` and
#: ``user -> user-target -> target``) collapse to the union of legal
#: target scopes per source:
#:
#: * ``user`` -> ``user-tenant`` or ``user-target`` (operator picks
#:   the axis).
#: * ``user-tenant`` -> ``tenant`` (operator ladder).
#: * ``user-target`` -> ``target`` (target ladder).
#: * ``tenant`` / ``target`` -> no further widening; the modal does
#:   not render a Promote button at all (the route returns 400 if a
#:   caller still POSTs).
PROMOTE_TARGETS_BY_SOURCE: Final[dict[MemoryScope, tuple[MemoryScope, ...]]] = {
    MemoryScope.USER: (MemoryScope.USER_TENANT, MemoryScope.USER_TARGET),
    MemoryScope.USER_TENANT: (MemoryScope.TENANT,),
    MemoryScope.USER_TARGET: (MemoryScope.TARGET,),
    MemoryScope.TENANT: (),
    MemoryScope.TARGET: (),
}


async def render_promote_modal(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    scope: MemoryScope,
    slug: str,
) -> HTMLResponse:
    """Render the HTMX-loaded promote modal fragment.

    Loads the source entry under read RBAC (404 on missing OR not
    visible -- info-leak avoidance) and lists the legal target scopes
    via :data:`PROMOTE_TARGETS_BY_SOURCE`. When the source scope is
    terminal (``tenant`` / ``target``), the route returns 400 -- the
    detail page only renders the Promote button when the source
    scope has at least one legal target.
    """
    service = MemoryService()
    entry = await service.recall(operator=operator, scope=scope, slug=slug, target_name=None)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory_not_found")

    legal_targets = PROMOTE_TARGETS_BY_SOURCE.get(entry.scope, ())
    if not legal_targets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"scope {entry.scope.value!r} has no broader scope to promote to "
                "(tenant + target are terminal on their ladders)"
            ),
        )

    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **build_common_template_context(session_ctx, csrf_token),
        "entry": {
            "scope": entry.scope.value,
            "slug": entry.slug,
            "target_name": entry.target_name,
        },
        "legal_targets": [
            {"value": target.value, "label": scope_label(target)} for target in legal_targets
        ],
        "target_scoped_values": [s.value for s in TARGET_SCOPED],
        "target_name_max_length": TARGET_NAME_MAX_LENGTH,
    }
    response = get_templates().TemplateResponse(request, "memory/_promote_modal.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


def _bind_audit_for_promote(
    *,
    session_ctx: UISessionContext,
    source_scope: MemoryScope,
    slug: str,
    target_scope: MemoryScope,
) -> None:
    """Bind contextvars so the promote audit row commits under the canonical op id.

    Same shape ``/api/v1/memory/{scope}/{slug}/promote`` (REST) uses
    -- ``audit_op_id`` is ``"memory.promote"`` (G5.2 contract);
    ``audit_promotion_target_scope`` distinguishes the row from a
    regular ``memory.remember`` write for G8 forensic queries.
    """
    structlog.contextvars.bind_contextvars(
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        audit_op_id="memory.promote",
        audit_op_class="write",
        audit_scope=source_scope.value,
        audit_slug=slug,
        audit_promotion_target_scope=target_scope.value,
    )


def _map_promote_error(exc: Exception) -> HTTPException:
    """Translate a service-raised promotion error to the right HTTPException.

    Mirrors the dispatch :func:`meho_backplane.api.v1.memory.promote`
    uses so the REST + UI surfaces produce identical statuses on
    matching inputs:

    * :class:`InvalidPromotionStepError` -> 400 (ladder-illegal pair).
    * :class:`PermissionDeniedError` -> 403 (canonical detail
      ``insufficient_promotion_authority``).
    * :class:`NotImplementedError` -> 501 (G0.3 #224 per-target ACL gap).
    * :class:`ValueError` starting ``promote_target_conflict`` -> 409;
      other ValueErrors -> 422.
    """
    if isinstance(exc, InvalidPromotionStepError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, PermissionDeniedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient_promotion_authority",
        )
    if isinstance(exc, NotImplementedError):
        return HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"not_implemented: {exc}",
        )
    if isinstance(exc, ValueError):
        message = str(exc)
        if message.startswith("promote_target_conflict"):
            return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=message,
        )
    # Defensive: caller filters by exception type. Re-raise so the
    # chassis ``ServerErrorMiddleware`` builds the canonical 500.
    raise exc


async def promote_entry(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    source_scope: MemoryScope,
    source_slug: str,
    target_scope: MemoryScope,
    target_name: str | None,
) -> HTMLResponse:
    """Promote one memory and HX-Redirect to the new (broader-scope) detail page.

    Submit handler for ``POST /ui/memory/<scope>/<slug>/promote``.
    Delegates ladder + authority checks to G5.2's
    :func:`assert_can_promote` (via :meth:`MemoryService.promote`)
    and surfaces the result as an HX-Redirect to the new detail URL.

    Idempotency contract is inherited from :meth:`MemoryService.promote`:
    a second click on Confirm with the same target redirects to the
    same URL because the service returns the existing target row (no
    insert, no conflict).
    """
    _bind_audit_for_promote(
        session_ctx=session_ctx,
        source_scope=source_scope,
        slug=source_slug,
        target_scope=target_scope,
    )
    service = MemoryService()
    try:
        entry = await service.promote(
            operator=operator,
            source_scope=source_scope,
            source_slug=source_slug,
            target_scope=target_scope,
            move=False,
            target_name=target_name,
        )
    except (
        InvalidPromotionStepError,
        PermissionDeniedError,
        NotImplementedError,
        ValueError,
    ) as exc:
        raise _map_promote_error(exc) from exc

    if entry is None:
        # 404 across "absent" and "not visible" preserves the
        # tenant-boundary info-leak avoidance contract the REST
        # promote route holds.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory_not_found",
        )

    redirect_url = f"/ui/memory/{entry.scope.value}/{entry.slug}"
    _log.info(
        "ui_memory_promote",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        source_scope=source_scope.value,
        target_scope=target_scope.value,
        slug=source_slug,
    )
    del request  # not consumed -- HX-Redirect needs no request context.
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": redirect_url},
    )
