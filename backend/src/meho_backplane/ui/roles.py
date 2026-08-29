# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared BFF role-lift for ``/ui/*`` read surfaces.

:class:`~meho_backplane.ui.auth.middleware.UISessionContext` carries only
``operator_sub`` + ``tenant_id``; the ``tenant_admin`` vs ``operator``
distinction a gated affordance needs (the audit replay pivot, the drawer
lineage links) is not on it. Resolving the role means decrypting the
stored access token and re-running the chassis JWT chain.

The audit-query console (:mod:`meho_backplane.ui.routes.audit.routes`)
established this fail-soft lift; the broadcast event drawer now shares it
so both drawers gate the replay deep-link identically. Extracting it here
keeps one implementation rather than a second per-surface copy (the
runbooks surface keeps its own historical copy; unifying that is out of
scope for this change).
"""

from __future__ import annotations

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.auth.refresh import (
    load_fresh_session,
    verify_access_token_with_refresh,
)

__all__ = ["is_ui_tenant_admin", "resolve_ui_operator"]

log = structlog.get_logger(__name__)


async def resolve_ui_operator(session_ctx: UISessionContext) -> Operator | None:
    """Re-verify the session's access token to lift the operator's role.

    Decrypts the stored access token and re-runs the chassis JWT chain via
    :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`,
    which silently refreshes once on the ``token_expired`` 401 before
    re-verifying, so an expired-but-refreshable token lifts the real role
    instead of degrading the operator mid-session.

    Fails **soft**: any hiccup (session row vanished between the middleware
    check and here, JWKS transiently unreachable, refresh unavailable,
    identity mismatch on the decoded token) returns ``None`` -- the caller
    then treats the request as a plain operator. An unavailable role lift
    must never 5xx a read surface.
    """
    try:
        decrypted = await load_fresh_session(session_ctx.session_id)
        if decrypted is None:
            return None
        settings = get_settings()
        _refreshed, operator = await verify_access_token_with_refresh(
            decrypted,
            expected_audience=settings.keycloak_audience,
        )
    except Exception as exc:
        log.info(
            "ui_role_lift_unavailable",
            session_id=str(session_ctx.session_id),
            reason=type(exc).__name__,
        )
        return None
    # A token whose identity diverges from the session row is a security
    # anomaly; treat it as "no admin" rather than honouring the elevated
    # claim (any gated affordance stays disabled).
    if operator.sub != session_ctx.operator_sub or operator.tenant_id != session_ctx.tenant_id:
        log.warning(
            "ui_role_lift_identity_mismatch",
            session_sub=session_ctx.operator_sub,
            token_sub=operator.sub,
        )
        return None
    return operator


async def is_ui_tenant_admin(session_ctx: UISessionContext) -> bool:
    """Resolve whether the session's operator is a ``tenant_admin``.

    Thin wrapper over :func:`resolve_ui_operator` returning just the admin
    verdict. Fails soft to ``False`` (operator privileges) so a gated
    affordance is disabled whenever the role lift can't complete; the
    write/replay surfaces re-check server-side, so a forged-enabled
    affordance still 403s there.
    """
    operator = await resolve_ui_operator(session_ctx)
    return operator is not None and operator.tenant_role is TenantRole.TENANT_ADMIN
