# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-role lift for the agents console surface.

Initiative #1824 (G10.8 Agents console), Task #1825 (T1). The chassis
:class:`~meho_backplane.ui.auth.middleware.UISessionContext` carries
``operator_sub`` + ``tenant_id`` only; the agents surface needs the
operator's :attr:`~meho_backplane.auth.operator.Operator.tenant_role`
to:

* hide the create / edit / enable-disable / delete affordances from
  non-admins on the list + detail templates (UX hint), and
* gate the actual create / edit / toggle / delete write handlers
  server-side with a 403 a crafted POST cannot bypass.

The lift mirrors the pattern
:mod:`~meho_backplane.ui.routes.connectors.operator` ships for the
connectors surface: read the encrypted BFF session row, decrypt the
access token, and re-validate it through the chassis JWT chain to
produce a full :class:`~meho_backplane.auth.operator.Operator` carrying
``tenant_role``. The JWT round-trip catches a same-session role
demotion the next time the operator browses the surface; storing a
cached role on the session row would extend the demotion's effective
window to the session's absolute TTL.

Two dependencies:

* :func:`resolve_role_probe` -- the read-path dependency (list +
  detail). Fails **soft**: any JWT-validation hiccup (session row
  gone, JWKS endpoint unreachable, token swapped) projects to a
  "no privileges" probe rather than 5xx-ing the page, so the
  read surface keeps rendering. The write handlers remain the
  security authority.
* :func:`resolve_operator_or_403` -- the write-path dependency
  (create / edit / toggle / delete). Returns the full
  :class:`Operator` after asserting
  :class:`~meho_backplane.auth.operator.TenantRole.TENANT_ADMIN`; a
  non-admin caller raises 403.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from fastapi import HTTPException, Request, status

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session

__all__ = [
    "OperatorRoleProbe",
    "resolve_operator_or_403",
    "resolve_role_probe",
    "resolve_run_operator_or_403",
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OperatorRoleProbe:
    """Tiny shape carrying the role booleans the agents templates read.

    The list + detail templates only need to know whether to render the
    create / edit / enable-disable / delete affordances (tenant_admin
    only). The write-route 403 gate is the server-side authority -- this
    shape is a UX hint that hides controls an operator can't use.
    """

    is_tenant_admin: bool
    is_operator: bool


def _probe_from_operator(operator: Operator) -> OperatorRoleProbe:
    """Project a full :class:`Operator` into the booleans the templates read."""
    return OperatorRoleProbe(
        is_tenant_admin=operator.tenant_role == TenantRole.TENANT_ADMIN,
        is_operator=operator.tenant_role in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN),
    )


async def _load_access_token_or_401(session_ctx: UISessionContext) -> str:
    """Load the encrypted session row, return the decrypted access token.

    The session middleware already validated the cookie before this
    helper runs; if the row vanished in the gap (revoked / expired
    while the request was in-flight), raise 401 so the operator's
    browser knows to re-authenticate. Mirrors the connectors /
    memory operator lifts.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        decrypted = await load_session(db_session, session_ctx.session_id)
    if decrypted is None:
        _log.info("ui_agents_session_gone", session_id=str(session_ctx.session_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    return decrypted.access_token


def _assert_identity_matches(operator: Operator, session_ctx: UISessionContext) -> None:
    """Reject a token whose sub / tenant_id diverges from the session row.

    A mismatch is a security anomaly (token swapped under the same
    session row); it must surface as 401, not a silently-honoured
    cross-identity read.
    """
    if operator.sub != session_ctx.operator_sub or operator.tenant_id != session_ctx.tenant_id:
        _log.warning(
            "ui_agents_session_identity_mismatch",
            session_sub=session_ctx.operator_sub,
            session_tenant_id=str(session_ctx.tenant_id),
            token_sub=operator.sub,
            token_tenant_id=str(operator.tenant_id),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_identity_mismatch",
        )


async def _lift_operator(session_ctx: UISessionContext) -> Operator:
    """Lift the full :class:`Operator` from the BFF session row.

    Common helper for the read-only role probe and the write-gating
    403 dependency. The JWT chain caches the JWKS in process so the
    round-trip is local-only after the first hit.
    """
    access_token = await _load_access_token_or_401(session_ctx)
    settings = get_settings()
    operator = await verify_jwt_for_audience(
        f"Bearer {access_token}",
        expected_audience=settings.keycloak_audience,
    )
    _assert_identity_matches(operator, session_ctx)
    return operator


async def resolve_role_probe(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> OperatorRoleProbe:
    """FastAPI dependency: project the session's operator role into booleans.

    Used by the read-only ``GET /ui/agents`` + ``GET /ui/agents/{name}``
    handlers. Fails **soft**: a JWT-validation hiccup projects to a
    "no privileges" probe rather than 5xx-ing the page. The write
    handlers (create / edit / toggle / delete) remain the server-side
    authority -- a soft "no privileges" hint only hides the affordances
    optimistically; a crafted POST still hits the 403 gate.
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    try:
        operator = await _lift_operator(session_ctx)
    except Exception as exc:
        _log.info(
            "ui_agents_role_probe_unavailable",
            session_id=str(session_ctx.session_id),
            reason=type(exc).__name__,
        )
        return OperatorRoleProbe(is_tenant_admin=False, is_operator=False)
    return _probe_from_operator(operator)


async def resolve_operator_or_403(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> Operator:
    """FastAPI dependency: lift the operator + gate to tenant_admin.

    Used by the agent create / edit / enable-disable / delete write
    handlers. A non-admin call raises 403 with a structured detail so
    the HTMX response carries the gate-failure semantics back to the
    surface.
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    operator = await _lift_operator(session_ctx)
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent_write_requires_tenant_admin",
        )
    return operator


async def resolve_run_operator_or_403(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> Operator:
    """FastAPI dependency: lift the operator + gate to operator-or-above.

    Used by the run console's run-initiation ``POST`` and SSE bridge
    (T2 #1829). Running an agent is an **operator**-level action -- the
    same floor the REST surface applies
    (``POST /api/v1/agents/{name}/run`` requires
    :class:`~meho_backplane.auth.operator.TenantRole.OPERATOR`); it is
    *not* a tenant_admin-only write like definition CRUD. A ``read_only``
    operator is rejected with 403. The lifted :class:`Operator` is the
    tenant-isolation lever: the invoker call the run / stream handlers
    make is tenant-scoped through it, never through a request parameter.
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    operator = await _lift_operator(session_ctx)
    if operator.tenant_role not in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent_run_requires_operator",
        )
    return operator
