# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``resolve_ui_operator`` -- lift a full :class:`Operator` from a BFF session.

Initiative #341 (G10.4 Memory UI), Task #877 (T1). The chassis
:class:`~meho_backplane.ui.auth.middleware.UISessionContext` carries
``operator_sub`` + ``tenant_id`` only; the per-scope memory RBAC matrix
(:mod:`~meho_backplane.memory.rbac`) also needs
:attr:`~meho_backplane.auth.operator.Operator.tenant_role` to gate:

* edit-in-place rendering on the detail view (operator vs tenant_admin),
* the actual ``PATCH`` / ``DELETE`` handlers (re-checks the matrix
  server-side; never trusts a client-supplied role hint),
* the create / promote handlers T2 #878 will add on this same router.

The dependency loads the encrypted session row (via the same
:func:`~meho_backplane.ui.auth.session_store.load_session` the chassis
middleware uses), decrypts the access token, and re-runs it through
:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` to produce a
fully-validated :class:`Operator`. The JWT chain caches the JWKS in
process so the round-trip is local-only after the first hit.

Why not store the role on the session row
-----------------------------------------

Two reasons:

1. The role can change between login and now (a tenant admin demotes
   the operator to ``read_only`` mid-session). Re-validating the
   access token catches that the next time the operator's browser
   issues a memory write. Storing a cached role on the session row
   would extend the demotion's effective window to the session's
   absolute TTL.

2. The session row is encrypted with the BFF's Fernet key; widening
   its schema means a migration plus a re-encrypt step on every
   row. The current shape (encrypted tokens only) keeps the schema
   minimal and lets the chassis carry the BFF concern without
   memory-specific contamination.

The cost is one in-process JWT decode per write request (~µs once the
JWKS is cached). Read requests still build a synthesised
:class:`Operator` with :attr:`TenantRole.OPERATOR` directly from the
:class:`UISessionContext` because the RBAC matrix only blocks reads
on the per-row ``user_sub`` check, which doesn't need the role. The
:func:`build_read_operator` helper exposes that path for the list /
recall handlers.
"""

from __future__ import annotations

import structlog
from fastapi import HTTPException, Request, status

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session

__all__ = ["build_read_operator", "resolve_ui_operator"]

_log = structlog.get_logger(__name__)


def build_read_operator(session_ctx: UISessionContext) -> Operator:
    """Build a synthesised :class:`Operator` for read-only memory paths.

    The memory RBAC matrix's read branch
    (:meth:`~meho_backplane.memory.rbac.MemoryRbacResolver.can_read`)
    gates user-flavoured rows on the stored ``user_sub`` match and
    treats tenant / target rows as visible to every operator in the
    tenant -- the role is not consulted. Synthesising an
    :class:`Operator` with :attr:`TenantRole.OPERATOR` is therefore
    sound for ``list_memories`` / ``recall``. The chassis session
    middleware already enforced authentication; this helper avoids the
    JWT round-trip on every list refresh.

    The synthesised ``raw_jwt`` is a stable string (``"ui-session"``)
    so any consumer that introspects it for chain-of-custody logging
    can distinguish a UI-mediated read from an ``/api/*`` request
    (which carries a real JWT).
    """
    return Operator(
        sub=session_ctx.operator_sub,
        raw_jwt="ui-session",
        tenant_id=session_ctx.tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _load_session_or_401(session_ctx: UISessionContext) -> str:
    """Load the encrypted session row and return the decrypted access token.

    The session middleware already validated the cookie before this
    dependency runs; if the row vanished in the gap (revoked / expired
    while the request was in-flight), raise 401 so the operator's
    browser knows to re-authenticate. Centralised so the JWT-decode
    branch of :func:`resolve_ui_operator` stays a one-liner.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        decrypted = await load_session(db_session, session_ctx.session_id)
    if decrypted is None:
        _log.info("ui_memory_session_gone", session_id=str(session_ctx.session_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    return decrypted.access_token


def _assert_token_matches_session(
    operator: Operator,
    session_ctx: UISessionContext,
) -> None:
    """Reject a token whose ``sub`` / ``tenant_id`` diverges from the session row.

    A mismatch is a security anomaly (token swapped under the same
    session row); it must surface as 401, not a silently-honoured
    cross-identity read.
    """
    if operator.sub != session_ctx.operator_sub or operator.tenant_id != session_ctx.tenant_id:
        _log.warning(
            "ui_memory_session_identity_mismatch",
            session_sub=session_ctx.operator_sub,
            session_tenant_id=str(session_ctx.tenant_id),
            token_sub=operator.sub,
            token_tenant_id=str(operator.tenant_id),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_identity_mismatch",
        )


async def resolve_ui_operator(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> Operator:
    """FastAPI dependency: lift the full :class:`Operator` from the BFF session.

    Used by every memory write handler (``PATCH`` / ``DELETE``). Reads
    the encrypted session row, decrypts the access token, and re-runs
    it through the chassis JWT chain to produce a fully-validated
    :class:`Operator` carrying :attr:`tenant_role`. Failure modes:

    * The session middleware should have rejected the request before
      this dependency runs; if the session row vanished between the
      middleware check and now (revoked / expired in the gap), this
      raises ``401`` with ``ui_session_required``.
    * The decrypted access token failing JWT validation
      (signature drift, expired, etc.) raises ``401`` with the chassis
      JWT chain's own ``detail`` code -- propagated unchanged so the
      operator can see the actual reason.

    Parameters
    ----------
    request:
        The FastAPI :class:`Request`. Used only to pull the chassis-
        bound :class:`UISessionContext` off ``request.state`` when the
        explicit ``session_ctx`` parameter isn't supplied.
    session_ctx:
        Optional pre-resolved session context. When ``None`` (the
        normal FastAPI dependency path), the helper invokes
        :func:`require_ui_session` to read it off ``request.state``.
    """
    if session_ctx is None:
        session_ctx = require_ui_session(request)
    access_token = await _load_session_or_401(session_ctx)
    settings = get_settings()
    operator = await verify_jwt_for_audience(
        f"Bearer {access_token}",
        expected_audience=settings.keycloak_audience,
    )
    _assert_token_matches_session(operator, session_ctx)
    return operator
