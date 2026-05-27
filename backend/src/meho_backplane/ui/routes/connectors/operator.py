# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-role lift for the connectors detail + probe routes.

Initiative #340 (G10.3 Connectors + Targets UI), Task #873 (T1). The
chassis :class:`~meho_backplane.ui.auth.middleware.UISessionContext`
carries ``operator_sub`` + ``tenant_id`` only; the connectors detail
template needs to know whether to render the re-probe button
(tenant_admin-only) and the probe handler needs to gate the write.

The lift mirrors the pattern
:mod:`~meho_backplane.ui.routes.memory.operator` ships for the
memory surface: read the encrypted session row, decrypt the access
token, re-validate it through the chassis JWT chain to produce a
full :class:`~meho_backplane.auth.operator.Operator` carrying
``tenant_role``. The JWT round-trip catches a same-session role
demotion the next time the operator browses the detail page; storing
a cached role on the session row would extend the demotion's
effective window to the session's absolute TTL.

Two surfaces:

* :class:`OperatorRoleProbe` -- a tiny shape carrying the booleans
  the detail template reads (``is_tenant_admin``). The full
  :class:`Operator` is heavier than the template needs and the
  detail route is read-only, so passing the probe shape (rather than
  the operator itself) keeps the template helper-free of auth
  internals.

* :func:`resolve_role_probe` -- the FastAPI dependency the detail
  handler uses. Used only for read paths (the probe handler uses
  :func:`resolve_operator_or_403` to get the full Operator + 403
  on a non-admin call).

* :func:`resolve_operator_or_403` -- the FastAPI dependency the
  probe handler uses. Returns the full :class:`Operator` after
  asserting :class:`~meho_backplane.auth.operator.TenantRole.TENANT_ADMIN`;
  a non-admin caller raises 403 with a structured detail so the
  HTMX response carries the gate-failure semantics the template's
  ``hx-on::after-request`` handler can branch on.
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
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OperatorRoleProbe:
    """Tiny shape carrying the role booleans the detail template reads.

    The connectors detail page only needs to know whether to render the
    re-probe button (tenant_admin-only). The probe RBAC gate is the
    server-side authority -- this shape is a UX hint that hides the
    affordance from operators who can't use it. The two role classes
    we surface today are ``tenant_admin`` (re-probe surface) and the
    catch-all ``operator`` (read surfaces); the dataclass leaves
    room for future role flags without rewriting the call sites.
    """

    is_tenant_admin: bool
    is_operator: bool


def _probe_from_operator(operator: Operator) -> OperatorRoleProbe:
    """Project a full :class:`Operator` into the booleans the template reads."""
    return OperatorRoleProbe(
        is_tenant_admin=operator.tenant_role == TenantRole.TENANT_ADMIN,
        is_operator=operator.tenant_role in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN),
    )


async def _load_access_token_or_401(session_ctx: UISessionContext) -> str:
    """Load the encrypted session row, return the decrypted access token.

    The session middleware already validated the cookie before this
    helper runs; if the row vanished in the gap (revoked / expired
    while the request was in-flight), raise 401 so the operator's
    browser knows to re-authenticate. Mirrors the same shape
    :func:`~meho_backplane.ui.routes.memory.operator._load_session_or_401`
    uses.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        decrypted = await load_session(db_session, session_ctx.session_id)
    if decrypted is None:
        _log.info("ui_connectors_session_gone", session_id=str(session_ctx.session_id))
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
            "ui_connectors_session_identity_mismatch",
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
    """FastAPI dependency: project the BFF session's operator role into
    the booleans the detail template reads.

    Used by the read-only ``GET /ui/connectors/{name}`` handler. Fails
    **soft**: any JWT-validation hiccup (session row gone, JWKS endpoint
    unreachable from this deploy, token swapped) projects to a
    "no privileges" probe rather than 5xx-ing the page. The probe
    handler (``POST /ui/connectors/{name}/probe``) remains the
    server-side authority -- a soft "no privileges" hint just hides
    the re-probe button optimistically; an attacker who crafts the
    POST with a forged form still hits the 403 gate on the write
    surface. The read surface must keep rendering even when the
    JWT round-trip can't complete (transient JWKS outage, etc.).
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    try:
        operator = await _lift_operator(session_ctx)
    except Exception as exc:
        _log.info(
            "ui_connectors_role_probe_unavailable",
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

    Used by ``POST /ui/connectors/{name}/probe``. A non-admin call
    raises 403 with the structured detail the HTMX response carries
    back to the template's ``hx-on::after-request`` so the surface
    can render the "insufficient privilege" alert without losing the
    rest of the page state.
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    operator = await _lift_operator(session_ctx)
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="re-probe requires tenant_admin",
        )
    return operator
