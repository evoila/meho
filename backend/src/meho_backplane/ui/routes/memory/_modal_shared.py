# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers + constants for the Memory UI create + promote modals.

Initiative #341 (G10.4 Memory UI), Task #878 (T2). The create modal
(:mod:`~meho_backplane.ui.routes.memory.create`) and the promote modal
(:mod:`~meho_backplane.ui.routes.memory.promote`) share:

* The scope-selector vocabulary -- :func:`writable_scopes_for`,
  :func:`scope_label`.
* The CSRF cookie set + the common template context -- both modals
  re-mint the chassis double-submit token on every render so a
  freshly-issued cookie lines up with the form's header echo.
* The form-field sizing constants -- :data:`TAGS_MAX_LENGTH`,
  :data:`TARGET_NAME_MAX_LENGTH`.

Pulled out of a hypothetical combined module so neither
:mod:`create` nor :mod:`promote` exceeds the chassis-wide ~600-line
cap, and so future shared modal surfaces (bulk delete, bulk extend,
...) can plug in without re-importing across sibling modules.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.memory import MemoryRbacResolver, MemoryScope
from meho_backplane.memory.schemas import TARGET_SCOPED
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME
from meho_backplane.ui.routes.memory.views import SLUG_MAX_LENGTH

__all__ = [
    "TAGS_MAX_LENGTH",
    "TARGET_NAME_MAX_LENGTH",
    "build_common_template_context",
    "parse_tags",
    "scope_label",
    "set_csrf_cookie",
    "writable_scopes_for",
]


#: Maximum length of the comma-separated tags input the create modal
#: accepts. The substrate has no fixed cap (``metadata.tags`` is a
#: JSON array on the row), but bounding the wire shape protects the
#: form-body parse against a paste-from-clipboard accident. 1 KiB is
#: well above the "couple of tags per memory" working-set the
#: consumer-needs.md §G5 surface uses.
TAGS_MAX_LENGTH: Final[int] = 1024


#: Maximum length of the operator-supplied ``target_name`` form field.
#: Mirrors the slug cap so the create-modal input shape mirrors the
#: ``/api/v1/memory`` POST body shape (``RememberBody.target_name``
#: there carries the same cap).
TARGET_NAME_MAX_LENGTH: Final[int] = SLUG_MAX_LENGTH


#: Comma-with-optional-whitespace splitter for the ``tags`` form
#: field. The substrate stores tags as a JSON array of distinct
#: strings; the create modal accepts the human shape (``a, b, c``)
#: and the helper below normalises to the substrate shape.
_TAGS_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\s*,\s*")


def parse_tags(raw: str | None) -> list[str]:
    """Split the comma-separated ``tags`` form field into the substrate shape.

    Empty / whitespace-only entries are dropped; duplicates are
    de-duplicated while preserving first-seen order so the operator's
    typing order survives the round-trip. Returns an empty list
    (never ``None``) when *raw* is missing or empty so the metadata
    builder doesn't need a separate ``None`` branch.
    """
    if not raw or not raw.strip():
        return []
    seen: set[str] = set()
    result: list[str] = []
    for part in _TAGS_SPLIT_RE.split(raw.strip()):
        cleaned = part.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def writable_scopes_for(operator: Operator) -> list[MemoryScope]:
    """Return the scopes *operator* may write to, in deterministic order.

    Consults the :class:`MemoryRbacResolver` ``can_write`` matrix one
    scope at a time. The order matches the ``MemoryScope`` enum
    declaration so the create modal renders the same scope list every
    render (no Python ``set`` ordering surprises).

    ``USER_TARGET`` and ``TARGET`` are listed only when the operator
    can write *some* row at that scope -- the resolver requires a
    ``target_name`` for those scopes but the *scope-selector* shape is
    "list the scopes the operator can in principle write to, then
    require ``target_name`` at submit". Passing a placeholder
    ``target_name="x"`` for the matrix check is the conservative
    posture: the resolver's role check fires identically for
    ``target_name=None`` and ``target_name="x"``; only the service-
    level required-field guard distinguishes them.
    """
    rbac = MemoryRbacResolver()
    candidates: list[MemoryScope] = []
    for scope in MemoryScope:
        target_name = "selector-probe" if scope in TARGET_SCOPED else None
        if rbac.can_write(operator, scope, target_name):
            candidates.append(scope)
    return candidates


def scope_label(scope: MemoryScope) -> str:
    """Translate a scope to its human-readable label for the selector."""
    return {
        MemoryScope.USER: "User",
        MemoryScope.USER_TENANT: "User x Tenant",
        MemoryScope.USER_TARGET: "User x Target",
        MemoryScope.TENANT: "Tenant",
        MemoryScope.TARGET: "Target",
    }[scope]


def set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the dashboard's CSRF cookie posture for state-changing pages.

    Local copy of :func:`meho_backplane.ui.routes.memory.views._set_csrf_cookie`
    because the views module's helper is module-private; centralising
    it across the memory modal surfaces here keeps the cookie attributes
    aligned across create + promote without a third-party import.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def build_common_template_context(
    session_ctx: UISessionContext,
    csrf_token: str,
) -> dict[str, object]:
    """Build the dict shared across every create / promote template render."""
    return {
        "page_title": "Memory",
        "active_surface": "memory",
        "ready": False,
        "operator_sub": session_ctx.operator_sub,
        "csrf_token": csrf_token,
    }
