# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write-boundary validator for ``AgentDefinition.identity_ref``.

G11.2-T8 (#1099) under Initiative #803. Extracted out of
:mod:`meho_backplane.agents.service` (G11.2-T9 #1112) as a sibling of
:func:`meho_backplane.agents.schemas.validate_name`: both are pure
write-boundary gates that the service layer applies before a row lands.
``validate_name`` is fully synchronous (a regex check) so it lives next
to the Pydantic models in :mod:`~meho_backplane.agents.schemas`;
``validate_identity_ref`` needs an
:class:`~sqlalchemy.ext.asyncio.AsyncSession` to look up
:class:`~meho_backplane.db.models.AgentPrincipal` rows, so it lives in
its own module rather than dragging an SQLAlchemy import into
``schemas.py``.

The function is re-exported from
:mod:`meho_backplane.agents.service` under the original
``_validate_identity_ref`` name so existing call sites (and the test
matrix that targets them) don't need to change.

Isolation level note
--------------------

The validator runs inside the caller's session, so the SELECT and the
subsequent INSERT/UPDATE share one transaction. Under PostgreSQL's
default :pep:`READ COMMITTED <PEP-0008>`-equivalent isolation level
(each statement gets its own snapshot, the chassis does not configure
``REPEATABLE READ``), a revoke that lands between the SELECT and the
write *is* visible to the write statement — there is a small TOCTOU
window between the validate and the write. The runtime-time check in
G11.3's ``run_scheduled`` (which enforces ``identity_ref ==
agent_client_id`` under the ``client_credentials`` grant) is the
authoritative gate; this validator is the write-time hygiene check that
keeps a typo'd or never-existed ``identity_ref`` from landing in the
first place.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AgentPrincipal

__all__ = [
    "AgentIdentityRefInvalidError",
    "validate_identity_ref",
]


class AgentIdentityRefInvalidError(Exception):
    """Raised when ``identity_ref`` does not resolve to a tenant-scoped principal.

    The :attr:`identity_ref` attribute carries the rejected value so the
    boundary layer can echo it back; the :attr:`reason` attribute
    distinguishes the failure mode (``unknown`` -- no row matches;
    ``revoked`` -- a row matches but its kill switch has been pulled)
    for the structlog breadcrumb. Both the REST route and the MCP tool
    map this exception to a structured 4xx with detail
    ``identity_ref_unknown`` -- a single code is intentional: leaking
    ``revoked`` vs ``unknown`` to an unauthenticated caller would expose
    whether a specific Keycloak client id ever existed in the tenant,
    which is exactly the cross-tenant existence leak ``agent:test-bot``
    reconnaissance would exploit. Operators see the structured
    ``reason`` in the :event:`identity_ref_invalid` warning emitted just
    before this exception is raised.
    """

    def __init__(self, identity_ref: str, reason: str) -> None:
        self.identity_ref = identity_ref
        self.reason = reason
        super().__init__(
            f"identity_ref {identity_ref!r} does not resolve to a registered "
            f"non-revoked agent principal in this tenant (reason={reason})"
        )


async def validate_identity_ref(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    identity_ref: str,
) -> None:
    """Reject *identity_ref* unless it names a non-revoked tenant-scoped principal.

    Matches exactly against :attr:`AgentPrincipal.keycloak_client_id`
    (no wildcards, no case-folding) inside a tenant-scoped WHERE so a
    cross-tenant probe is invisible to the query and rejected as
    ``unknown`` -- the existence of another tenant's principal is never
    leaked. ``revoked`` rows are rejected with ``reason="revoked"``;
    the boundary collapses both reasons into a single
    ``identity_ref_unknown`` code (to avoid leaking whether a specific
    clientId ever existed), but operators see the precise reason on
    the :event:`identity_ref_invalid` warning emitted before each raise.

    The query runs inside the *caller's* session so validate + write
    share one transaction; see the module docstring for the TOCTOU
    window note. The logger is resolved per-call via
    :func:`structlog.get_logger` to stay capturable under
    :func:`structlog.testing.capture_logs` -- same precedent + rationale
    as :mod:`meho_backplane.auth.rbac` and :mod:`meho_backplane.auth.jwt`.
    """
    result = await session.execute(
        select(AgentPrincipal.revoked).where(
            AgentPrincipal.tenant_id == tenant_id,
            AgentPrincipal.keycloak_client_id == identity_ref,
        )
    )
    revoked = result.scalar_one_or_none()
    if revoked is None:
        structlog.get_logger(__name__).warning(
            "identity_ref_invalid",
            identity_ref=identity_ref,
            reason="unknown",
            tenant_id=str(tenant_id),
        )
        raise AgentIdentityRefInvalidError(identity_ref, reason="unknown")
    if revoked:
        structlog.get_logger(__name__).warning(
            "identity_ref_invalid",
            identity_ref=identity_ref,
            reason="revoked",
            tenant_id=str(tenant_id),
        )
        raise AgentIdentityRefInvalidError(identity_ref, reason="revoked")
