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
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.memory.rbac import (
    _PROMOTION_LADDER,
    InvalidPromotionStepError,
    MemoryRbacResolver,
    PermissionDeniedError,
    assert_can_promote,
)
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


# ---------------------------------------------------------------------------
# G5.2-T3 (#625) -- assert_can_promote + _PROMOTION_LADDER
# ---------------------------------------------------------------------------

# The full set of MemoryScope pairs (source != target excluded ladder-wise
# but included here for completeness -- the helper rejects identity steps
# as invalid). Used to drive parametrised non-ladder coverage so any new
# ladder entry we add later forces an explicit allow on this test.
_ALL_SCOPE_PAIRS: list[tuple[MemoryScope, MemoryScope]] = [
    (src, dst) for src in MemoryScope for dst in MemoryScope
]

# The four legal ladder steps. Pin them inline rather than re-import the
# private constant so a future widening of _PROMOTION_LADDER without
# explicit test sign-off surfaces here.
_EXPECTED_LADDER: frozenset[tuple[MemoryScope, MemoryScope]] = frozenset(
    {
        (MemoryScope.USER, MemoryScope.USER_TENANT),
        (MemoryScope.USER_TENANT, MemoryScope.TENANT),
        (MemoryScope.USER, MemoryScope.USER_TARGET),
        (MemoryScope.USER_TARGET, MemoryScope.TARGET),
    }
)


def test_promotion_ladder_constant_matches_expected_steps() -> None:
    """The ladder is exactly the four documented widenings -- no more, no fewer.

    Pinning the constant here is load-bearing: T4's promote route reads
    the same constant to validate the request scope-pair before authz,
    and a silent widening of the ladder (e.g. adding ``user-tenant -> target``)
    would let cross-ladder promotion through. The test enforces that any
    ladder change forces an explicit test sign-off.
    """
    assert _PROMOTION_LADDER == _EXPECTED_LADDER


def test_promotion_ladder_never_contains_identity_steps() -> None:
    """Promotion is strictly broadening; same-scope rewrite is not promotion."""
    for scope in MemoryScope:
        assert (scope, scope) not in _PROMOTION_LADDER


def test_promotion_ladder_is_acyclic_no_demotion_steps() -> None:
    """For every (src, dst) in the ladder, (dst, src) is never present.

    A reverse-direction step would let an operator silently demote a
    tenant memory to a user memory under the same authority check.
    Demotion is explicitly out of scope per Initiative #374; the
    ladder shape enforces it.
    """
    for src, dst in _PROMOTION_LADDER:
        assert (dst, src) not in _PROMOTION_LADDER


@pytest.mark.parametrize(
    "src, dst",
    sorted(
        (pair for pair in _ALL_SCOPE_PAIRS if pair not in _EXPECTED_LADDER),
        # `MemoryScope` is a StrEnum so the value form sorts deterministically;
        # the result is the same on macOS / Linux / CI.
        key=lambda p: (p[0].value, p[1].value),
    ),
)
def test_non_ladder_pairs_raise_invalid_promotion_step(src: MemoryScope, dst: MemoryScope) -> None:
    """Every non-ladder pair is rejected with the request-shape exception.

    Drives the full negative space: identity steps, cross-ladder steps
    (``user-tenant -> target``, ``user-target -> tenant``), and
    backward steps. All must surface as :class:`InvalidPromotionStepError`
    (so T4's route maps to **400**, not 403). The role used here is
    ``TENANT_ADMIN`` to prove that even the maximally-privileged
    operator cannot bypass the ladder shape via authz.
    """
    operator = _op(TenantRole.TENANT_ADMIN)
    with pytest.raises(InvalidPromotionStepError):
        # target_id is supplied for both target-scoped pairs and ignored
        # otherwise; sending it unconditionally keeps the parametrisation
        # uniform across all pair shapes.
        assert_can_promote(operator, src, dst, target_id="some-target")


def test_operator_can_promote_user_to_user_tenant_within_tenant() -> None:
    """Acceptance criterion #2: operator role passes user -> user-tenant.

    The tenant-boundary check (operator and source share ``tenant_id``)
    lives in the T4 route, not this helper -- the helper trusts the
    caller has resolved that already.
    """
    operator = _op(TenantRole.OPERATOR)
    assert assert_can_promote(operator, MemoryScope.USER, MemoryScope.USER_TENANT) is None


def test_operator_can_promote_user_to_user_target_when_target_id_supplied() -> None:
    """`user -> user-target` passes for an operator with a target_id."""
    operator = _op(TenantRole.OPERATOR)
    assert (
        assert_can_promote(
            operator,
            MemoryScope.USER,
            MemoryScope.USER_TARGET,
            target_id="rdc-vcenter",
        )
        is None
    )


def test_user_target_promotion_without_target_id_raises_invalid_step() -> None:
    """The helper enforces the target_id precondition itself.

    The T4 route loads target_id from the request body and passes it
    through; a programming-error caller that forgets the kwarg surfaces
    here as an :class:`InvalidPromotionStepError` so the missing-target
    case maps to **400** (request error), matching the response shape
    a route-level Pydantic validation would have produced.
    """
    operator = _op(TenantRole.OPERATOR)
    with pytest.raises(InvalidPromotionStepError, match="requires target_id"):
        assert_can_promote(
            operator,
            MemoryScope.USER,
            MemoryScope.USER_TARGET,
            target_id=None,
        )


def test_tenant_admin_can_promote_user_tenant_to_tenant() -> None:
    """Acceptance criterion #3: tenant_admin passes `* -> tenant`."""
    operator = _op(TenantRole.TENANT_ADMIN)
    assert assert_can_promote(operator, MemoryScope.USER_TENANT, MemoryScope.TENANT) is None


def test_operator_promoting_to_tenant_raises_permission_denied() -> None:
    """Acceptance criterion #3: non-admin operator -> tenant is denied 403.

    Asserts both the exception type (:class:`PermissionDeniedError`,
    **not** :class:`HTTPException` -- T4's route owns the HTTP mapping)
    and the carried scope / reason so T4 can pattern-match the reason
    field for its 403 detail body.
    """
    operator = _op(TenantRole.OPERATOR)
    with pytest.raises(PermissionDeniedError) as excinfo:
        assert_can_promote(operator, MemoryScope.USER_TENANT, MemoryScope.TENANT)
    assert excinfo.value.scope is MemoryScope.TENANT
    assert excinfo.value.reason == "insufficient_promotion_authority"


def test_insufficient_promotion_authority_log_line_fields() -> None:
    """The denial path emits a structured event mirroring ``insufficient_role``.

    Fields are pinned: ``operator_sub`` / ``source_scope`` / ``target_scope``
    (matching the task body acceptance criterion). The event name
    ``insufficient_promotion_authority`` is the discriminator on-call uses
    to separate route-level role denials (``insufficient_role`` from
    :func:`auth.rbac.require_role`) from promotion-step authz denials.
    """
    operator = _op(TenantRole.OPERATOR, sub="op-denied")
    with capture_logs() as captured, pytest.raises(PermissionDeniedError):
        assert_can_promote(operator, MemoryScope.USER_TENANT, MemoryScope.TENANT)
    promotion_events = [
        line for line in captured if line.get("event") == "insufficient_promotion_authority"
    ]
    assert len(promotion_events) == 1
    line = promotion_events[0]
    assert line["operator_sub"] == "op-denied"
    assert line["source_scope"] == "user-tenant"
    assert line["target_scope"] == "tenant"
    assert line["log_level"] == "warning"


def test_read_only_operator_cannot_promote_user_to_user_tenant() -> None:
    """Read-only role is denied every promotion across every scope.

    The role's name is load-bearing for the rest of the backplane;
    promotion is no exception. Surfaces as :class:`PermissionDeniedError`
    (mapped to 403 by T4), not :class:`InvalidPromotionStepError`, because
    the ladder step itself is legal -- the operator just lacks the
    role floor.
    """
    operator = _op(TenantRole.READ_ONLY)
    with pytest.raises(PermissionDeniedError) as excinfo:
        assert_can_promote(operator, MemoryScope.USER, MemoryScope.USER_TENANT)
    assert excinfo.value.reason == "insufficient_promotion_authority"


def test_read_only_operator_cannot_promote_user_to_user_target() -> None:
    """Read-only role is denied user-target promotion even with target_id."""
    operator = _op(TenantRole.READ_ONLY)
    with pytest.raises(PermissionDeniedError):
        assert_can_promote(
            operator,
            MemoryScope.USER,
            MemoryScope.USER_TARGET,
            target_id="rdc-vcenter",
        )


def test_target_promotion_without_tenant_admin_raises_not_implemented() -> None:
    """Acceptance criterion #5: `* -> target` per-target-ACL branch fails loud.

    G0.3 #224 has not shipped per-target ACLs. The v0.2 contract leaves
    the gap explicit: non-admin promotion to ``target`` raises
    :class:`NotImplementedError` (with a message naming #224) rather
    than a silent 403. Surfaces the missing plumbing to the on-call
    rather than masking it as an authz denial.
    """
    operator = _op(TenantRole.OPERATOR)
    with pytest.raises(NotImplementedError, match=r"G0\.3 \#224"):
        assert_can_promote(
            operator,
            MemoryScope.USER_TARGET,
            MemoryScope.TARGET,
            target_id="rdc-vcenter",
        )


def test_tenant_admin_target_promotion_v0_2_fallback() -> None:
    """Acceptance criterion #5: tenant_admin is the v0.2 fallback for `* -> target`.

    The G0.3 ACL hook stays unbuilt until #224 lands; until then,
    promoting to ``target`` requires ``tenant_admin``. This test pins
    that fallback so T4's route can rely on it -- without it, every
    target-shared promote would 500 in v0.2.
    """
    operator = _op(TenantRole.TENANT_ADMIN)
    assert (
        assert_can_promote(
            operator,
            MemoryScope.USER_TARGET,
            MemoryScope.TARGET,
            target_id="rdc-vcenter",
        )
        is None
    )


def test_helper_is_standalone_not_a_dependency_factory() -> None:
    """Acceptance criterion #6: callable directly, no FastAPI plumbing.

    The helper takes an :class:`Operator` and two scopes and returns
    ``None`` on success -- there is no ``Depends`` wrapper, no async
    handle, no request object. T4's route loads the operator + source
    row + target before calling this; the standalone shape is the
    point.
    """
    operator = _op(TenantRole.OPERATOR)
    result = assert_can_promote(operator, MemoryScope.USER, MemoryScope.USER_TENANT)
    # Plain function returning None on success -- no awaitable, no
    # FastAPI Response, no implicit state.
    assert result is None
    # The function is directly callable; not a factory returning a
    # callable like require_role.
    assert callable(assert_can_promote)


def test_memory_rbac_module_has_no_fastapi_import() -> None:
    """Acceptance criterion #3 negative-import assertion.

    The domain-exception convention requires :mod:`memory.rbac` to be
    FastAPI-free -- the route maps :class:`PermissionDeniedError` to
    403 at the HTTP boundary, not in this module. Catching an
    accidental ``from fastapi import HTTPException`` regression here
    keeps the layering rule auditable in CI rather than reviewer
    attention.
    """
    import meho_backplane.memory.rbac as rbac_module

    source = rbac_module.__file__ or ""
    assert source.endswith("rbac.py")
    # Read the source rather than trusting the module dict: a transitive
    # re-export (e.g. via from .schemas import *) wouldn't appear in
    # `dir(rbac_module)` but a textual scan of the import block catches
    # any direct dependency from this layer on FastAPI.
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    assert "import fastapi" not in text
    assert "from fastapi" not in text
