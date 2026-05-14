# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RBAC matrix tests for :class:`MemoryRbacResolver`.

G5.1-T1 (#421) acceptance criteria coverage:

* The matrix is the contract -- every row of "who can write what
  scope" / "who can read what scope" gets one parametrised case.
* The matrix is **pure** (no DB, no I/O), so the tests run as plain
  functions with no fixtures; ``operator`` instances are built
  inline.
* The :func:`MemoryRbacResolver.visible_kinds` enumeration is locked
  in -- callers (``list_memories`` / ``search_memories``) plug the
  list into SQL ``IN (...)`` clauses; a regression that changes the
  order or drops a value would break the substrate filter silently.
"""

from __future__ import annotations

import uuid

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.memory.rbac import MemoryRbacResolver
from meho_backplane.memory.schemas import MemoryScope


def _op(role: TenantRole, *, sub: str = "op-42") -> Operator:
    """Build an :class:`Operator` with the requested role.

    Minimal helper so test bodies stay readable -- the JWT / tenant
    machinery is irrelevant to RBAC unit tests. The ``raw_jwt`` field
    is required on the model but the resolver never reads it.
    """
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=role,
    )


# ---------------------------------------------------------------------------
# can_write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role, scope, target_name, expected",
    [
        # Operator-level role: user-flavoured scopes pass; tenant denied;
        # target needs target_name; missing target_name denies.
        (TenantRole.OPERATOR, MemoryScope.USER, None, True),
        (TenantRole.OPERATOR, MemoryScope.USER_TENANT, None, True),
        (TenantRole.OPERATOR, MemoryScope.USER_TARGET, "infra-1", True),
        (TenantRole.OPERATOR, MemoryScope.USER_TARGET, None, False),
        (TenantRole.OPERATOR, MemoryScope.TENANT, None, False),
        (TenantRole.OPERATOR, MemoryScope.TARGET, "infra-1", True),
        (TenantRole.OPERATOR, MemoryScope.TARGET, None, False),
        # Tenant-admin role: every scope passes (with the target_name
        # guard still in place for target-scoped writes).
        (TenantRole.TENANT_ADMIN, MemoryScope.USER, None, True),
        (TenantRole.TENANT_ADMIN, MemoryScope.USER_TENANT, None, True),
        (TenantRole.TENANT_ADMIN, MemoryScope.USER_TARGET, "infra-1", True),
        (TenantRole.TENANT_ADMIN, MemoryScope.USER_TARGET, None, False),
        (TenantRole.TENANT_ADMIN, MemoryScope.TENANT, None, True),
        (TenantRole.TENANT_ADMIN, MemoryScope.TARGET, "infra-1", True),
        # Read-only role: every write denied.
        (TenantRole.READ_ONLY, MemoryScope.USER, None, False),
        (TenantRole.READ_ONLY, MemoryScope.USER_TENANT, None, False),
        (TenantRole.READ_ONLY, MemoryScope.USER_TARGET, "infra-1", False),
        (TenantRole.READ_ONLY, MemoryScope.TENANT, None, False),
        (TenantRole.READ_ONLY, MemoryScope.TARGET, "infra-1", False),
    ],
)
def test_can_write_matrix(
    role: TenantRole, scope: MemoryScope, target_name: str | None, expected: bool
) -> None:
    """One parametrised case per matrix cell -- the contract is the table."""
    resolver = MemoryRbacResolver()
    assert resolver.can_write(_op(role), scope, target_name) is expected


# ---------------------------------------------------------------------------
# can_read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope, user_sub, expected",
    [
        # Operator reading their own user-scoped memory: allowed.
        (MemoryScope.USER, "op-42", True),
        (MemoryScope.USER_TENANT, "op-42", True),
        (MemoryScope.USER_TARGET, "op-42", True),
        # Operator reading someone else's user-scoped memory: denied.
        (MemoryScope.USER, "op-other", False),
        (MemoryScope.USER_TENANT, "op-other", False),
        (MemoryScope.USER_TARGET, "op-other", False),
        # Corrupt row (user-scoped with no user_sub stored): denied
        # rather than wildcard-allow.
        (MemoryScope.USER, None, False),
    ],
)
def test_can_read_user_scoped(scope: MemoryScope, user_sub: str | None, expected: bool) -> None:
    """User-flavoured reads gated by ``operator.sub == stored.user_sub``."""
    resolver = MemoryRbacResolver()
    assert resolver.can_read(_op(TenantRole.OPERATOR), scope, user_sub, "any-target") is expected


@pytest.mark.parametrize("role", list(TenantRole))
@pytest.mark.parametrize("scope", [MemoryScope.TENANT, MemoryScope.TARGET])
def test_can_read_tenant_and_target_scopes_are_open(role: TenantRole, scope: MemoryScope) -> None:
    """Tenant + target reads are open to any role in the tenant.

    Includes ``read_only`` -- tenant-shared knowledge is the
    substrate's point ("team becomes the unit of memory" per
    consumer-needs.md §G5 L131); gating it on role would defeat the
    compounding-knowledge property.
    """
    resolver = MemoryRbacResolver()
    assert resolver.can_read(_op(role), scope, user_sub=None, target_name="infra-1") is True


# ---------------------------------------------------------------------------
# visible_kinds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(TenantRole))
def test_visible_kinds_is_stable_for_every_role(role: TenantRole) -> None:
    """Every role sees every memory kind; the per-row RBAC filter is what gates.

    The list shape is locked in for two reasons: callers plug it into
    SQL ``IN (...)`` clauses (a missing kind would silently hide a
    whole scope from list/search), and the ordering matches
    :class:`MemoryScope` iteration so tests can pin without sorting.
    """
    resolver = MemoryRbacResolver()
    assert resolver.visible_kinds(_op(role)) == [
        "memory-user",
        "memory-user-tenant",
        "memory-user-target",
        "memory-tenant",
        "memory-target",
    ]
