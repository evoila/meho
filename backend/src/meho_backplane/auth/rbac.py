# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RBAC primitive — :func:`require_role` FastAPI dependency factory.

The G0.1 RBAC surface is intentionally small: three roles
(:class:`~meho_backplane.auth.operator.TenantRole.READ_ONLY`,
:class:`~meho_backplane.auth.operator.TenantRole.OPERATOR`,
:class:`~meho_backplane.auth.operator.TenantRole.TENANT_ADMIN`), one
ordering, one dependency. Routes that require a minimum role declare
``Depends(require_role(TenantRole.OPERATOR))`` (or ``TENANT_ADMIN``);
the dependency runs after :func:`verify_jwt_and_bind` succeeds, reads
``operator.tenant_role``, and raises HTTP 403 if the operator's role
ranks below the required minimum.

Role ordering is **explicit**, not implicit in the StrEnum. Iterating
over :class:`TenantRole` gives definition order, but that contract is
fragile — a future enum reorder, an alphabetical sort, or a docstring-
driven refactor could silently invert ranking. Pinning the order in
:data:`_ROLE_ORDER` makes the comparison rule a single auditable line.

Linearity is a v0.2 acceptance: the three roles fit a strict total
order. Future policy work (orthogonal `auditor`, ABAC, approval
workflows) will replace this primitive entirely; widening the enum
without updating :data:`_ROLE_ORDER` is caught by
:func:`require_role`'s `KeyError`-raising lookup at first call,
not silently misranked at runtime. Tests in ``test_auth_rbac.py`` pin
the ordering so a regression surfaces in CI.

The dependency factory shape (`require_role(role) -> Callable`) is the
canonical FastAPI idiom for parameterised dependencies — see
https://fastapi.tiangolo.com/tutorial/dependencies/sub-dependencies/
and `fastapi.Security` for the same pattern with scopes. Returning the
:class:`Operator` from the dependency (rather than `None`) lets routes
that opt into RBAC also receive the validated operator without
declaring `verify_jwt_and_bind` separately.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, status

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.middleware import verify_jwt_and_bind

__all__ = ["authorize_tenant_scope", "require_approvals_access", "require_role"]

#: Linear role ordering. Index = rank; ``read_only`` is rank 0,
#: ``tenant_admin`` is rank 2. The dependency compares the actual
#: operator's rank to the required minimum's rank.
#:
#: Pinning the order here (rather than iterating the StrEnum)
#: decouples ranking from definition order in
#: :class:`~meho_backplane.auth.operator.TenantRole`. A future enum
#: reorder, alphabetical sort, or attribute-style refactor cannot
#: silently invert ranking; a deletion would surface at first call as
#: a :class:`ValueError` from :meth:`tuple.index`, which the factory
#: re-raises eagerly so a misconfigured deployment fails at import
#: rather than drifting into 500s on first authenticated request.
_ROLE_ORDER: Final[tuple[TenantRole, ...]] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)


def require_role(min_role: TenantRole) -> Callable[[Operator], Operator]:
    """Build a FastAPI dependency that enforces a minimum tenant role.

    Returns a callable suitable for ``Depends(...)`` whose body reads
    the validated :class:`Operator` produced by
    :func:`verify_jwt_and_bind`, compares ``operator.tenant_role``
    against *min_role* using the linear ordering in
    :data:`_ROLE_ORDER`, and either returns the operator (route is
    authorised) or raises HTTP 403 ``insufficient_role``.

    The minimum-role rank is resolved **once at factory call time**
    rather than on every request: the lookup is a tuple search, so the
    micro-cost is negligible, but resolving early surfaces an unknown
    role (e.g. a typo in a router declaration, or a role added to the
    enum without updating :data:`_ROLE_ORDER`) as an import-time
    :class:`ValueError` instead of a per-request 500. The actual
    operator's rank is resolved per-request because the JWT contents
    vary; an unknown role on the operator side is impossible by
    construction (`verify_jwt` rejects it at 401), but the same
    `tuple.index` call will raise loudly if that invariant ever drifts.

    Usage::

        @router.post(
            "/conventions",
            dependencies=[Depends(require_role(TenantRole.TENANT_ADMIN))],
        )
        async def create_convention(...): ...

    Or, when the route handler also needs the operator instance::

        @router.get("/admin/audit")
        async def list_audit(
            operator: Operator = Depends(require_role(TenantRole.TENANT_ADMIN)),
        ) -> ...: ...

    Args:
        min_role: The minimum role an operator must hold to pass the
            dependency. ``OPERATOR`` accepts both ``OPERATOR`` and
            ``TENANT_ADMIN``; ``TENANT_ADMIN`` accepts only
            ``TENANT_ADMIN``; ``READ_ONLY`` accepts everyone (rare —
            most routes that allow read-only just don't need the gate).

    Returns:
        A FastAPI dependency callable. Call sites pass it to
        :func:`fastapi.Depends`.

    Raises:
        ValueError: At factory call time, if *min_role* is not in
            :data:`_ROLE_ORDER`. Indicates a missing entry in the
            ordering tuple after the enum was widened — a coding bug,
            not a runtime error.
    """
    min_rank = _ROLE_ORDER.index(min_role)

    def _checker(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
        actual_rank = _ROLE_ORDER.index(operator.tenant_role)
        if actual_rank < min_rank:
            # Resolve the logger per-call rather than from a module-
            # level proxy. The module-level shape interacts badly with
            # `cache_logger_on_first_use=True` (the production default
            # in `meho_backplane.logging.configure_logging`): the
            # cached BoundLogger pins a reference to the
            # `_CONFIG.default_processors` list it was built with, and
            # later `structlog.configure(...)` calls *replace* (not
            # mutate) that reference — so test fixtures that swap the
            # processor chain (`structlog.testing.capture_logs`,
            # per-test `configure` + `reset_defaults`) cannot reach
            # the orphaned reference the cached logger still holds.
            # Same precedent + rationale as
            # `meho_backplane.retrieval.embedding.EmbeddingService`
            # (see ``docs/codebase/backend.md``). Per-call cost is a
            # few microseconds; acceptable on the 403 path which is
            # not latency-critical. Originally exposed by the
            # `-n 3 --dist loadscope` flake in #738.
            #
            # Bind the structured fields explicitly rather than
            # relying on the enum's StrEnum __str__ — keeps the log
            # line shape stable if the enum representation changes.
            structlog.get_logger(__name__).warning(
                "insufficient_role",
                operator_sub=operator.sub,
                actual_role=operator.tenant_role.value,
                required_role=min_role.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient_role",
            )
        return operator

    return _checker


#: Rank of ``operator`` in :data:`_ROLE_ORDER`, resolved once at import.
#: The approvals plane admits ``operator``-or-higher; below that rank a
#: principal needs the orthogonal ``approver`` capability (#3243).
#: Resolving at import (rather than per-request) gives the same fail-fast
#: property :func:`require_role` gets from factory-time resolution — an
#: enum widening that misses :data:`_ROLE_ORDER` surfaces as an import-time
#: :class:`ValueError`, not a per-request 500.
_OPERATOR_RANK: Final[int] = _ROLE_ORDER.index(TenantRole.OPERATOR)


def require_approvals_access(
    operator: Operator = Depends(verify_jwt_and_bind),
) -> Operator:
    """FastAPI dependency gating the approvals plane (#3243).

    The approvals plane — ``list`` / ``show`` / ``approve`` / ``reject`` /
    ``decide`` (``/api/v1/approvals*`` except the principal-scoped
    ``/result`` read) — is reachable by a principal that is **either**:

    * ``operator``-or-higher on the linear :class:`TenantRole` lattice
      (the pre-#3243 behaviour — ``operator`` and ``tenant_admin`` keep
      approvals access unchanged), **or**
    * carries the orthogonal ``approver`` capability
      (``operator.approver``), regardless of tenant role.

    This **decouples the decide right from the dispatch right**. A
    dedicated approver principal provisioned as ``read_only`` role +
    ``approver=true`` clears this gate yet is denied
    ``POST /api/v1/operations/call`` — dispatch stays
    ``Depends(require_role(TenantRole.OPERATOR))`` and ``read_only`` ranks
    below ``operator`` — so it can clear a four-eyes gate without holding
    execution rights. The flag never widens the linear lattice: dispatch,
    admin, and every other ``require_role``-gated surface are untouched,
    and the ``sub``-keyed self-approval refusal (#1401) still applies
    (an approver cannot approve a request it parked).

    A principal below ``operator`` **and** lacking the flag is rejected
    with HTTP 403 ``insufficient_role`` — the same wire token
    :func:`require_role` returns, so clients matching on it keep working —
    plus a structured ``insufficient_approvals_access`` log line carrying
    ``operator_sub``, ``actual_role``, and ``approver``.

    Unlike :func:`require_role` this is a plain dependency (no minimum-role
    parameter to bind), so it is used directly as
    ``Depends(require_approvals_access)`` rather than via a factory call.
    """
    actual_rank = _ROLE_ORDER.index(operator.tenant_role)
    if actual_rank >= _OPERATOR_RANK or operator.approver:
        return operator
    structlog.get_logger(__name__).warning(
        "insufficient_approvals_access",
        operator_sub=operator.sub,
        actual_role=operator.tenant_role.value,
        approver=operator.approver,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="insufficient_role",
    )


def authorize_tenant_scope(operator: Operator, requested: UUID | None) -> UUID:
    """Resolve the effective tenant for a request, authorizing a cross-tenant claim.

    Returns ``operator.tenant_id`` when *requested* is ``None`` or equals
    the operator's own tenant — the common, same-tenant path. A request
    naming a *different* tenant is the cross-tenant case: it is allowed
    **only** for a platform-admin (``operator.platform_admin``, the
    cross-tenant capability primitive #1638) and otherwise raises HTTP 403
    ``cross_tenant_requires_platform_admin``.

    This replaces the prior per-route gate that allowed the claim on
    ``tenant_admin`` *rank* alone — a tenant-admin of tenant A could pass
    tenant B's id and read/act on B (a cross-tenant IDOR). Tenant role
    governs *what* an operator may do within their tenant; crossing the
    tenant boundary is a separate, platform-level capability.
    """
    if requested is None or requested == operator.tenant_id:
        return operator.tenant_id
    if operator.platform_admin:
        return requested
    structlog.get_logger(__name__).warning(
        "cross_tenant_denied",
        operator_sub=operator.sub,
        operator_tenant_id=str(operator.tenant_id),
        requested_tenant_id=str(requested),
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="cross_tenant_requires_platform_admin",
    )
