# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-role lift for the conventions UI surface.

Initiative #1838 (G10.12 Conventions console), Task #1895 (T1). The
read page at ``/ui/conventions`` needs two things off the BFF session:

* a full :class:`~meho_backplane.auth.operator.Operator` to pass to
  :class:`~meho_backplane.conventions.service.ConventionsService` (the
  service keys every query + the preamble-budget arithmetic on
  ``operator.tenant_id`` / ``operator.sub``), and
* an ``is_tenant_admin`` UX hint so the template can reveal the T2
  author / edit / delete affordances only to operators who can use
  them (the write routes T2 adds remain the server-side authority).

The read surface is OPERATOR-tier (mirrors the REST
``GET /api/v1/conventions`` ``require_role(OPERATOR)`` gate and the
runbooks read/write split). Rather than re-derive the BFF auth seams,
this module composes the two existing lifts:

* :func:`meho_backplane.ui.routes.memory.operator.build_read_operator`
  synthesises an OPERATOR-tier :class:`Operator` from the
  :class:`~meho_backplane.ui.auth.middleware.UISessionContext` -- enough
  for the tenant-scoped ``ConventionsService`` reads, and avoids a JWT
  round-trip on every list refresh (the conventions read path is
  tenant-scoped and not role-gated below operator).
* :func:`meho_backplane.ui.routes.connectors.operator.resolve_role_probe`
  is the **soft** role probe: any JWT-validation hiccup (session row
  gone, JWKS endpoint unreachable, token swapped) projects to
  ``is_tenant_admin=False`` rather than 5xx-ing the page. Hiding an
  affordance optimistically is safe -- T2's write routes hold the real
  403 gate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.connectors.operator import (
    resolve_operator_or_403,
    resolve_role_probe,
)
from meho_backplane.ui.routes.memory.operator import build_read_operator

__all__ = [
    "ConventionsReadContext",
    "ConventionsWriteContext",
    "resolve_read_context",
    "resolve_write_context",
]


@dataclass(frozen=True)
class ConventionsReadContext:
    """The operator + admin-hint + session-id triple the read routes thread.

    ``operator`` is a synthesised OPERATOR-tier
    :class:`~meho_backplane.auth.operator.Operator` -- enough for the
    tenant-scoped ``ConventionsService`` reads. ``is_tenant_admin`` is
    a soft UX hint that gates the T2 author / edit / delete affordances
    in the template; the write routes re-check role server-side.
    ``session_id`` is carried so the T2 modal renders (loaded via the
    read context) can mint a CSRF double-submit token for the write
    form they embed.
    """

    operator: Operator
    is_tenant_admin: bool
    session_id: uuid.UUID


@dataclass(frozen=True)
class ConventionsWriteContext:
    """The full operator + session-id pair the T2 write routes thread.

    ``operator`` is the real JWT-lifted
    :class:`~meho_backplane.auth.operator.Operator` carrying
    ``tenant_role`` -- resolved through
    :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`,
    which 403s a non-``TENANT_ADMIN`` caller before the handler runs (the
    server-side write gate). ``session_id`` lets a re-rendered modal mint
    a fresh CSRF token.
    """

    operator: Operator
    session_id: uuid.UUID


async def resolve_read_context(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> ConventionsReadContext:
    """FastAPI dependency: lift the read operator + the admin UX hint."""
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    operator = build_read_operator(session_ctx)
    probe = await resolve_role_probe(request, session_ctx)
    return ConventionsReadContext(
        operator=operator,
        is_tenant_admin=probe.is_tenant_admin,
        session_id=session_ctx.session_id,
    )


async def resolve_write_context(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> ConventionsWriteContext:
    """FastAPI dependency: lift the full operator + gate to tenant_admin.

    Used by every T2 write route. A non-admin caller raises 403 inside
    :func:`resolve_operator_or_403` before the handler body runs -- the
    server-side authority the read context's soft ``is_tenant_admin``
    hint only optimistically mirrors in the template.
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    operator = await resolve_operator_or_403(request, session_ctx)
    return ConventionsWriteContext(
        operator=operator,
        session_id=session_ctx.session_id,
    )
