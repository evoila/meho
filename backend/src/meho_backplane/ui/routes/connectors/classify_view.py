# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/ui/policy/classify`` -- edit the classification of operations in the console.

A tenant admin sets the category, labels or "always wait" mark of one
operation, one group, one connector, or many operations by pattern. The
page works in two steps, like every other surface
(:mod:`meho_backplane.policy.bulk`):

* ``POST /ui/policy/classify/preview`` -- the dry run: every operation the
  match finds, with its values before and after. Nothing is written.
* ``POST /ui/policy/classify/request`` -- ask for the change. It is a
  change of rights, so it waits until another tenant admin approves it on
  the approvals page.

The review drawer links here with the match filled in (``?connector=``,
``&group=`` or ``&op=``). Gated to ``tenant_admin``; the POSTs pass the
CSRF check of every console form.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.policy.bulk import (
    ClassificationEditError,
    ClassificationOffError,
    preview_classification_edit,
    request_classification_edit,
)
from meho_backplane.policy.store import (
    ClassificationEdit,
    classification_enabled,
    load_classification,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_operator
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403
from meho_backplane.ui.routes.connectors.review_drawer import _set_csrf_cookie
from meho_backplane.ui.templating import get_templates

__all__ = ["build_classify_router", "parse_labels"]

_require_operator_dep = Depends(require_ui_operator)
_require_admin_dep = Depends(resolve_operator_or_403)
_MATCH_MAX = 200
_LABELS_MAX = 32 * 320


def parse_labels(text: str | None) -> dict[str, str] | None:
    """Read ``key=value`` lines; ``None`` for an empty field (labels stay as they are)."""
    if text is None or not text.strip():
        return None
    labels: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ClassificationEditError(f"Write each label as key=value, not {line.strip()!r}.")
        labels[key.strip()] = value.strip()
    return labels


def _always_wait(value: str | None) -> bool | None:
    return {"true": True, "false": False}.get((value or "").strip())


def _edit(
    connector: str | None,
    group: str | None,
    op: str | None,
    category: str | None,
    labels: str | None,
    always_wait: str | None,
) -> ClassificationEdit:
    match = {"connector": connector, "group": group, "op": op}
    return ClassificationEdit(
        match={key: value.strip() for key, value in match.items() if value and value.strip()},
        category=(category or "").strip() or None,
        labels=parse_labels(labels),
        always_wait=_always_wait(always_wait),
    )


def _fragment(request: Request, context: dict[str, Any]) -> HTMLResponse:
    full = {"error": None, "preview": None, "requested": None, "form": {}, **context}
    return get_templates().TemplateResponse(request, "policy/_classify_result.html", full)


async def _classify_page(
    request: Request,
    connector: str = Query(default="", max_length=_MATCH_MAX),
    group: str = Query(default="", max_length=_MATCH_MAX),
    op: str = Query(default="", max_length=_MATCH_MAX),
    session_ctx: UISessionContext = _require_operator_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """Render the edit form, with the match from the query filled in."""
    enabled = classification_enabled()
    categories: list[str] = []
    if enabled:
        async with get_sessionmaker()() as session:
            taxonomy, _ = await load_classification(session, operator.tenant_id)
        categories = list(taxonomy.categories)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "policy/classify.html",
        {
            "enabled": enabled,
            "categories": categories,
            "match": {"connector": connector, "group": group, "op": op},
            "csrf_token": csrf_token,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def _preview(
    request: Request,
    connector: str | None = Form(default=None, max_length=_MATCH_MAX),
    group: str | None = Form(default=None, max_length=_MATCH_MAX),
    op: str | None = Form(default=None, max_length=_MATCH_MAX),
    category: str | None = Form(default=None, max_length=63),
    labels: str | None = Form(default=None, max_length=_LABELS_MAX),
    always_wait: str | None = Form(default=None, max_length=5),
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """The dry run: every matched operation, before and after. Writes nothing."""
    form = {
        "connector": connector or "",
        "group": group or "",
        "op": op or "",
        "category": category or "",
        "labels": labels or "",
        "always_wait": always_wait or "",
    }
    try:
        preview = await preview_classification_edit(
            operator, _edit(connector, group, op, category, labels, always_wait)
        )
    except (ClassificationOffError, ClassificationEditError) as exc:
        return _fragment(request, {"error": str(exc)})
    return _fragment(request, {"preview": preview.to_dict(), "form": form})


async def _request(
    request: Request,
    connector: str | None = Form(default=None, max_length=_MATCH_MAX),
    group: str | None = Form(default=None, max_length=_MATCH_MAX),
    op: str | None = Form(default=None, max_length=_MATCH_MAX),
    category: str | None = Form(default=None, max_length=63),
    labels: str | None = Form(default=None, max_length=_LABELS_MAX),
    always_wait: str | None = Form(default=None, max_length=5),
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """Ask for the change. It waits until another tenant admin approves it."""
    try:
        result = await request_classification_edit(
            operator, _edit(connector, group, op, category, labels, always_wait)
        )
    except (ClassificationOffError, ClassificationEditError) as exc:
        return _fragment(request, {"error": str(exc)})
    extras = result.get("extras") or {}
    return _fragment(
        request,
        {
            "requested": {
                "status": result.get("status"),
                "approval_request_id": extras.get("approval_request_id"),
                "error": result.get("error"),
                "matched_operations": result["matched_operations"],
            }
        },
    )


def build_classify_router() -> APIRouter:
    """Construct the classify page router (a factory, like its siblings)."""
    router = APIRouter(tags=["ui-policy"])
    router.add_api_route(
        "/ui/policy/classify",
        _classify_page,
        methods=["GET"],
        name="ui_policy_classify",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/policy/classify/preview",
        _preview,
        methods=["POST"],
        name="ui_policy_classify_preview",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/policy/classify/request",
        _request,
        methods=["POST"],
        name="ui_policy_classify_request",
        response_class=HTMLResponse,
    )
    return router
