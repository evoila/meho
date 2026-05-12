# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stub routes for RBAC end-to-end verification — disabled by default.

This module ships **two** trivial GET endpoints that exist solely to
exercise :func:`~meho_backplane.auth.rbac.require_role` end-to-end
through the FastAPI dependency graph: a 200 path, a 403 path, and the
``insufficient_role`` log assertion. Real product routes will declare
``Depends(require_role(...))`` themselves as they land in G3 / G4 / G5
/ G7 / G8 / G9; T4 only ships the primitive plus this verification
surface.

The router is **opt-in**. :data:`Settings.enable_rbac_test_route`
(env var ``MEHO_ENABLE_RBAC_TEST_ROUTE``) defaults to ``False``;
:mod:`meho_backplane.main` consults it at import time and only calls
``app.include_router(rbac_test.router)`` when the flag is set. Two
consequences fall out:

1. **Production deploys are inert.** Without the env var, the routes
   404 — the ``/api/v1/rbac-test`` prefix is genuinely unmounted. There
   is no "disabled but reachable" surface that could be probed for
   shape; this is the same inert-by-default discipline applied to the
   federation-proof secret read in :mod:`api.v1.health`.
2. **CI flips the flag for the integration test job only.** A
   ``MEHO_ENABLE_RBAC_TEST_ROUTE=1`` env var in the RBAC integration
   pipeline mounts the routes; the unit tests in
   ``tests/test_auth_rbac.py`` build their own ``TestClient`` with
   the flag on (they cannot rely on the module-level ``app`` because
   that was constructed before the env var was set).

The alternative — gating via a pytest-only conftest mount — was
considered and rejected: it works for unit tests but cannot exercise
the dependency end-to-end through the *deployed* app shape, which is
the failure mode this stub specifically guards against (a route that
authorises correctly in the test suite but mis-mounts in production).

Routes return a tiny JSON dict with no operator-controllable strings;
the response body intentionally carries the operator ``sub`` and
tenant id so an integration test can assert the dependency wired the
correct :class:`Operator` through, not just "some 200".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/rbac-test", tags=["rbac-test"])

# Module-level dependency singletons.
#
# ``require_role`` is a function factory: every call builds a fresh
# closure. Calling it inside a parameter default
# (``operator: Operator = Depends(require_role(...))``) trips ruff's
# B008 because the call would happen at every route-function definition
# evaluation. Building the closure once at module import gives FastAPI
# a stable reference to ``Depends`` against, matches the standard
# FastAPI pattern for parameterised dependencies, and lets B008 stay
# strict for genuine accidental-call defaults.
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))
_require_operator = Depends(require_role(TenantRole.OPERATOR))


@router.get("/admin")
async def admin_only(operator: Operator = _require_admin) -> dict[str, str]:
    """Stub route — passes only for ``tenant_admin``.

    Used to verify that ``require_role(TENANT_ADMIN)`` rejects
    ``operator`` and ``read_only`` with HTTP 403 + the
    ``insufficient_role`` log line.
    """
    return {
        "ok": "true",
        "operator_sub": operator.sub,
        "tenant_id": str(operator.tenant_id),
    }


@router.get("/operator")
async def operator_or_admin(operator: Operator = _require_operator) -> dict[str, str]:
    """Stub route — passes for ``operator`` and ``tenant_admin``.

    Used to verify the linear-ordering rule: ``operator`` passes the
    operator-or-higher gate, ``tenant_admin`` also passes (admin >=
    operator), ``read_only`` is rejected with 403.
    """
    return {
        "ok": "true",
        "operator_sub": operator.sub,
        "tenant_id": str(operator.tenant_id),
    }
