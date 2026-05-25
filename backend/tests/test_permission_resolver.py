# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the G11.2-T3 permission resolver.

Coverage matrix (Task #820 acceptance criteria):

* Safety-level defaults — no AgentPermission rows:
  ``safe`` → ``auto-execute``, ``caution`` → ``needs-approval``,
  ``dangerous`` → ``deny``, unknown safety_level → ``deny``.
* Explicit permission row overrides:
  row ``auto-execute`` on a ``safe`` op → ``auto-execute``;
  row ``needs-approval`` on a ``safe`` op → ``needs-approval``;
  row ``deny`` on a ``safe`` op → ``deny``.
* Safety-level ceiling enforcement — a row cannot loosen beyond the
  ceiling: ``auto-execute`` row on a ``caution`` op → ``needs-approval``;
  ``auto-execute`` row on a ``dangerous`` op → ``needs-approval`` (a
  destructive op is grantable up to human approval, never auto-executed);
  ``dangerous`` op with **no** grant → ``deny`` (default unchanged).
* Tie-break — two equally-specific matching patterns fold to the most
  restrictive verdict (fail-closed, order-independent).
* Specificity honours ``?`` / ``[`` glob metachars (not just ``*``), so
  an over-matching pattern cannot masquerade as a full-length literal.
* Role ceiling — ``READ_ONLY`` principal with a ``needs-approval`` row
  on a ``safe`` op → ``needs-approval`` (role ceiling caps at
  ``needs-approval``); ``OPERATOR`` / ``TENANT_ADMIN`` are uncapped.
* Pattern specificity — more-specific patterns win over wildcards;
  exact match beats glob prefix.
* Target-scope matching — row with ``target_scope=<uuid>`` only matches
  that target; row with ``target_scope=None`` / ``"*"`` matches any
  target; row with mismatched scope is ignored.
* Cross-tenant isolation — rows for one tenant are not visible to
  principals from another tenant.
* Reason string carries verdict source so agents can diagnose refusals.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.permissions import resolve_verdict
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, PermissionVerdict, Tenant
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / env
# ---------------------------------------------------------------------------

_TENANT_ID: uuid.UUID = uuid.UUID("10000000-0000-0000-0000-000000000001")
_OTHER_TENANT_ID: uuid.UUID = uuid.UUID("20000000-0000-0000-0000-000000000002")
_PRINCIPAL_SUB: str = "agent:resolver-test"
_CREATED_BY: str = "admin:resolver-test"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars Settings requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operator(
    *,
    sub: str = _PRINCIPAL_SUB,
    tenant_id: uuid.UUID = _TENANT_ID,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    """Construct an :class:`Operator` for tests — no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Resolver Test Agent",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
    )


async def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    """Insert a Tenant row so FK constraints pass."""
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _insert_permission(
    *,
    tenant_id: uuid.UUID = _TENANT_ID,
    principal_sub: str = _PRINCIPAL_SUB,
    op_pattern: str,
    verdict: str,
    target_scope: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Insert one AgentPermission row for use in a test."""
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                op_pattern=op_pattern,
                verdict=verdict,
                target_scope=target_scope,
                created_by_sub=_CREATED_BY,
                expires_at=expires_at,
            )
        )
        await session.commit()


async def _resolve(
    *,
    sub: str = _PRINCIPAL_SUB,
    tenant_id: uuid.UUID = _TENANT_ID,
    role: TenantRole = TenantRole.OPERATOR,
    op_id: str = "vault.kv.read",
    safety_level: str = "safe",
    target_id: Any = None,
) -> tuple[PermissionVerdict, str]:
    """Run :func:`resolve_verdict` against the test DB."""
    operator = _make_operator(sub=sub, tenant_id=tenant_id, role=role)
    async with get_sessionmaker()() as session:
        return await resolve_verdict(
            session=session,
            operator=operator,
            op_id=op_id,
            safety_level=safety_level,
            target_id=target_id,
        )


# ---------------------------------------------------------------------------
# Phase 0 — seed tenants (once per module)
# ---------------------------------------------------------------------------

# The conftest autouse fixture runs alembic upgrade head per test, giving each
# test a fresh DB. Tenant rows must therefore be seeded inside the test body or
# in an async fixture that runs after the DB reset.

# ---------------------------------------------------------------------------
# Safety-level defaults (no permission rows)
# ---------------------------------------------------------------------------


async def test_safe_op_auto_executes_by_default() -> None:
    """``safety_level='safe'`` with no rows → ``auto-execute``."""
    verdict, reason = await _resolve(safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE
    assert "safety_level default" in reason


async def test_caution_op_needs_approval_by_default() -> None:
    """``safety_level='caution'`` with no rows → ``needs-approval``."""
    verdict, reason = await _resolve(safety_level="caution")
    assert verdict == PermissionVerdict.NEEDS_APPROVAL
    assert "safety_level default" in reason


async def test_dangerous_op_denied_by_default() -> None:
    """``safety_level='dangerous'`` with no rows → ``deny``."""
    verdict, reason = await _resolve(safety_level="dangerous")
    assert verdict == PermissionVerdict.DENY
    assert "safety_level default" in reason


async def test_unknown_safety_level_denied_fail_closed() -> None:
    """Unknown ``safety_level`` falls back to ``deny`` (fail-closed)."""
    verdict, _ = await _resolve(safety_level="unknown-future-level")
    assert verdict == PermissionVerdict.DENY


# ---------------------------------------------------------------------------
# Explicit permission row overrides
# ---------------------------------------------------------------------------


async def test_explicit_auto_execute_row_overrides_caution_default() -> None:
    """Row with ``auto-execute`` verdict on a ``caution`` op — the row
    wins over the default but is then tightened by the safety ceiling to
    ``needs-approval``."""
    await _seed_tenant(_TENANT_ID, "t-auto-caution")
    await _insert_permission(op_pattern="vault.kv.*", verdict="auto-execute")
    # caution ceiling clamps auto-execute → needs-approval
    verdict, reason = await _resolve(safety_level="caution")
    assert verdict == PermissionVerdict.NEEDS_APPROVAL
    assert "ceiling" in reason


async def test_explicit_deny_row_on_safe_op() -> None:
    """Row with ``deny`` on a ``safe`` op → ``deny`` (row tightens default)."""
    await _seed_tenant(_TENANT_ID, "t-deny-safe")
    await _insert_permission(op_pattern="vault.kv.read", verdict="deny")
    verdict, reason = await _resolve(op_id="vault.kv.read", safety_level="safe")
    assert verdict == PermissionVerdict.DENY
    assert "permission row" in reason


async def test_explicit_needs_approval_row_on_safe_op() -> None:
    """Row with ``needs-approval`` on a ``safe`` op → ``needs-approval``."""
    await _seed_tenant(_TENANT_ID, "t-needs-safe")
    await _insert_permission(op_pattern="vault.kv.read", verdict="needs-approval")
    verdict, reason = await _resolve(op_id="vault.kv.read", safety_level="safe")
    assert verdict == PermissionVerdict.NEEDS_APPROVAL
    assert "permission row" in reason


# ---------------------------------------------------------------------------
# Safety-level ceiling enforcement
# ---------------------------------------------------------------------------


async def test_auto_execute_row_clamped_by_dangerous_ceiling() -> None:
    """``auto-execute`` row on a ``dangerous`` op → ``needs-approval``.

    A destructive op is grantable ("deny **unless granted**", #820) but
    never auto-executed: an explicit ``auto-execute`` grant is tightened
    to the ``dangerous`` ceiling of ``needs-approval`` (human-gated).
    """
    await _seed_tenant(_TENANT_ID, "t-ceil-dangerous")
    await _insert_permission(op_pattern="*", verdict="auto-execute")
    verdict, reason = await _resolve(safety_level="dangerous")
    assert verdict == PermissionVerdict.NEEDS_APPROVAL
    assert "ceiling" in reason


async def test_needs_approval_row_honoured_on_dangerous_op() -> None:
    """``needs-approval`` row on a ``dangerous`` op → ``needs-approval``.

    The grant sits exactly at the ``dangerous`` ceiling, so a destructive
    op an operator explicitly granted lands on human approval rather than
    a blanket deny.
    """
    await _seed_tenant(_TENANT_ID, "t-ceil-needs-dangerous")
    await _insert_permission(op_pattern="*", verdict="needs-approval")
    verdict, _reason = await _resolve(safety_level="dangerous")
    assert verdict == PermissionVerdict.NEEDS_APPROVAL


async def test_dangerous_op_with_no_grant_denies() -> None:
    """``dangerous`` op with no matching grant → ``deny`` (default).

    Only the *ceiling* moved to ``needs-approval``; the no-grant
    *default* for a destructive op stays ``deny``.
    """
    await _seed_tenant(_TENANT_ID, "t-dangerous-default")
    verdict, reason = await _resolve(safety_level="dangerous")
    assert verdict == PermissionVerdict.DENY
    assert "dangerous" in reason


async def test_expired_grant_ignored_by_resolver() -> None:
    """A grant past its ``expires_at`` no longer counts (G11.2-T6 #819).

    A time-bounded elevation reverts **at expiry**, before the sweeper
    deletes it: an expired ``deny`` grant on a ``safe`` op is ignored, so
    the resolver falls back to the ``safe`` default (``auto-execute``).
    """
    await _seed_tenant(_TENANT_ID, "t-expired-grant")
    past = datetime.now(UTC) - timedelta(hours=1)
    await _insert_permission(op_pattern="*", verdict="deny", expires_at=past)
    verdict, reason = await _resolve(safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE
    assert "safety_level default" in reason


async def test_active_time_bounded_grant_honoured() -> None:
    """A grant with a future ``expires_at`` still counts (G11.2-T6 #819).

    The mirror of the expired case: a future-expiry ``deny`` grant on a
    ``safe`` op is honoured (the elevation window is still open), so the
    resolver returns ``deny`` rather than the ``safe`` default.
    """
    await _seed_tenant(_TENANT_ID, "t-active-grant")
    future = datetime.now(UTC) + timedelta(hours=1)
    await _insert_permission(op_pattern="*", verdict="deny", expires_at=future)
    verdict, _reason = await _resolve(safety_level="safe")
    assert verdict == PermissionVerdict.DENY


async def test_equal_specificity_tie_breaks_to_most_restrictive() -> None:
    """Two equally-specific matching patterns fold to the most restrictive.

    ``"vault.*"`` (auto-execute) and ``"vault.[kd]v.read"`` (deny) both
    match ``vault.kv.read`` and both score specificity 6 (literal prefix
    ``"vault."``). The tie must resolve to ``deny`` (fail-closed) and must
    not depend on unordered row order.
    """
    await _seed_tenant(_TENANT_ID, "t-tie-break")
    await _insert_permission(op_pattern="vault.*", verdict="auto-execute")
    await _insert_permission(op_pattern="vault.[kd]v.read", verdict="deny")
    verdict, reason = await _resolve(op_id="vault.kv.read", safety_level="safe")
    assert verdict == PermissionVerdict.DENY
    assert "most-restrictive" in reason


async def test_question_mark_pattern_loses_to_exact_match() -> None:
    """A ``?`` glob does not masquerade as a full-length literal.

    ``"vault.k?.read"`` over-matches (``?`` is a wildcard) so it scores
    only its literal prefix ``"vault.k"`` (7); the exact
    ``"vault.kv.read"`` scores its full length (13) and wins. The exact
    grant's ``deny`` therefore beats the ``?``-pattern ``auto-execute``.
    """
    await _seed_tenant(_TENANT_ID, "t-qmark-specificity")
    await _insert_permission(op_pattern="vault.k?.read", verdict="auto-execute")
    await _insert_permission(op_pattern="vault.kv.read", verdict="deny")
    verdict, _reason = await _resolve(op_id="vault.kv.read", safety_level="safe")
    assert verdict == PermissionVerdict.DENY


async def test_auto_execute_row_clamped_by_caution_ceiling() -> None:
    """``auto-execute`` row on a ``caution`` op → ``needs-approval`` (ceiling)."""
    await _seed_tenant(_TENANT_ID, "t-ceil-caution")
    await _insert_permission(op_pattern="*", verdict="auto-execute")
    verdict, reason = await _resolve(safety_level="caution")
    assert verdict == PermissionVerdict.NEEDS_APPROVAL
    assert "ceiling" in reason


async def test_safe_op_has_no_ceiling() -> None:
    """``safe`` op has no ceiling — ``auto-execute`` row stays ``auto-execute``."""
    await _seed_tenant(_TENANT_ID, "t-no-ceil")
    await _insert_permission(op_pattern="*", verdict="auto-execute")
    verdict, _ = await _resolve(safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE


# ---------------------------------------------------------------------------
# Role ceiling
# ---------------------------------------------------------------------------


async def test_read_only_role_cannot_exceed_needs_approval() -> None:
    """``READ_ONLY`` principal with ``auto-execute`` row on a ``safe`` op
    → ``needs-approval`` (role ceiling applied)."""
    await _seed_tenant(_TENANT_ID, "t-readonly-role")
    await _insert_permission(op_pattern="*", verdict="auto-execute")
    verdict, reason = await _resolve(
        role=TenantRole.READ_ONLY,
        safety_level="safe",
    )
    assert verdict == PermissionVerdict.NEEDS_APPROVAL
    assert "role ceiling" in reason


async def test_read_only_with_no_rows_safe_op_needs_approval() -> None:
    """``READ_ONLY`` + no rows + ``safe`` op → ``needs-approval`` via role ceiling."""
    verdict, reason = await _resolve(
        role=TenantRole.READ_ONLY,
        safety_level="safe",
    )
    assert verdict == PermissionVerdict.NEEDS_APPROVAL
    assert "role ceiling" in reason


async def test_operator_role_uncapped() -> None:
    """``OPERATOR`` role has no ceiling — ``auto-execute`` row stays ``auto-execute``."""
    await _seed_tenant(_TENANT_ID, "t-operator-uncapped")
    await _insert_permission(op_pattern="*", verdict="auto-execute")
    verdict, _ = await _resolve(role=TenantRole.OPERATOR, safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE


async def test_tenant_admin_role_uncapped() -> None:
    """``TENANT_ADMIN`` role has no ceiling."""
    await _seed_tenant(_TENANT_ID, "t-admin-uncapped")
    await _insert_permission(op_pattern="*", verdict="auto-execute")
    verdict, _ = await _resolve(role=TenantRole.TENANT_ADMIN, safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE


# ---------------------------------------------------------------------------
# Pattern specificity — more-specific pattern wins
# ---------------------------------------------------------------------------


async def test_specific_pattern_beats_wildcard() -> None:
    """Exact ``op_id`` match beats a ``*`` wildcard when both rows exist."""
    await _seed_tenant(_TENANT_ID, "t-specificity")
    # Broad wildcard: deny everything.
    await _insert_permission(op_pattern="*", verdict="deny")
    # Narrow exact match: auto-execute this one op.
    await _insert_permission(op_pattern="vault.kv.read", verdict="auto-execute")
    # The exact match has higher specificity → auto-execute wins.
    verdict, reason = await _resolve(op_id="vault.kv.read", safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE
    assert "vault.kv.read" in reason


async def test_glob_prefix_beats_star_wildcard() -> None:
    """``vault.kv.*`` (prefix+glob) beats ``*`` when both match."""
    await _seed_tenant(_TENANT_ID, "t-glob-priority")
    await _insert_permission(op_pattern="*", verdict="deny")
    await _insert_permission(op_pattern="vault.kv.*", verdict="auto-execute")
    verdict, reason = await _resolve(op_id="vault.kv.list", safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE
    assert "vault.kv.*" in reason


async def test_non_matching_glob_falls_back_to_default() -> None:
    """A glob row that doesn't match the op_id is ignored; default fires."""
    await _seed_tenant(_TENANT_ID, "t-no-match")
    await _insert_permission(op_pattern="vcenter.*", verdict="deny")
    # "vault.kv.read" doesn't match "vcenter.*"; safety_level default applies.
    verdict, _ = await _resolve(op_id="vault.kv.read", safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE


# ---------------------------------------------------------------------------
# Target-scope matching
# ---------------------------------------------------------------------------

_TARGET_UUID: uuid.UUID = uuid.UUID("99000000-0000-0000-0000-000000000099")
_OTHER_TARGET: uuid.UUID = uuid.UUID("88000000-0000-0000-0000-000000000088")


async def test_target_scoped_row_matches_correct_target() -> None:
    """Row with ``target_scope=<uuid>`` grants only for that target."""
    await _seed_tenant(_TENANT_ID, "t-target-match")
    await _insert_permission(
        op_pattern="*",
        verdict="deny",
        target_scope=str(_TARGET_UUID),
    )
    # Correct target → row matches → deny.
    verdict, _ = await _resolve(safety_level="safe", target_id=_TARGET_UUID)
    assert verdict == PermissionVerdict.DENY


async def test_target_scoped_row_ignored_for_other_target() -> None:
    """Row scoped to one target does not fire for a different target."""
    await _seed_tenant(_TENANT_ID, "t-target-other")
    await _insert_permission(
        op_pattern="*",
        verdict="deny",
        target_scope=str(_TARGET_UUID),
    )
    # Different target → row doesn't match → safety_level default fires.
    verdict, _ = await _resolve(safety_level="safe", target_id=_OTHER_TARGET)
    assert verdict == PermissionVerdict.AUTO_EXECUTE


async def test_null_target_scope_matches_any_target() -> None:
    """Row with ``target_scope=None`` matches any target."""
    await _seed_tenant(_TENANT_ID, "t-null-scope")
    await _insert_permission(op_pattern="*", verdict="deny", target_scope=None)
    verdict, _ = await _resolve(safety_level="safe", target_id=_TARGET_UUID)
    assert verdict == PermissionVerdict.DENY


async def test_star_target_scope_matches_any_target() -> None:
    """Row with ``target_scope="*"`` matches any target (same as None)."""
    await _seed_tenant(_TENANT_ID, "t-star-scope")
    await _insert_permission(op_pattern="*", verdict="deny", target_scope="*")
    verdict, _ = await _resolve(safety_level="safe", target_id=_OTHER_TARGET)
    assert verdict == PermissionVerdict.DENY


async def test_target_scoped_row_no_target_id_is_ignored() -> None:
    """Row scoped to a specific target does not fire when dispatch has no target."""
    await _seed_tenant(_TENANT_ID, "t-target-none")
    await _insert_permission(
        op_pattern="*",
        verdict="deny",
        target_scope=str(_TARGET_UUID),
    )
    # Dispatch has no target → scoped row doesn't match → default fires.
    verdict, _ = await _resolve(safety_level="safe", target_id=None)
    assert verdict == PermissionVerdict.AUTO_EXECUTE


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_rows_from_other_tenant_are_invisible() -> None:
    """Permission rows for tenant B are not evaluated for principal from tenant A."""
    await _seed_tenant(_TENANT_ID, "t-iso-a")
    await _seed_tenant(_OTHER_TENANT_ID, "t-iso-b")
    # Insert a deny row under the *other* tenant.
    await _insert_permission(
        tenant_id=_OTHER_TENANT_ID,
        op_pattern="*",
        verdict="deny",
    )
    # Principal from _TENANT_ID sees no rows → safety_level default fires.
    verdict, _ = await _resolve(tenant_id=_TENANT_ID, safety_level="safe")
    assert verdict == PermissionVerdict.AUTO_EXECUTE


# ---------------------------------------------------------------------------
# Reason string content
# ---------------------------------------------------------------------------


async def test_reason_names_safety_default_source() -> None:
    """Reason string names the safety_level default when no row matches."""
    _, reason = await _resolve(safety_level="caution")
    assert "safety_level default" in reason
    assert "caution" in reason


async def test_reason_names_row_pattern_source() -> None:
    """Reason string names the op_pattern when a row fires."""
    await _seed_tenant(_TENANT_ID, "t-reason-row")
    await _insert_permission(op_pattern="vault.kv.read", verdict="deny")
    _, reason = await _resolve(op_id="vault.kv.read", safety_level="safe")
    assert "permission row" in reason
    assert "vault.kv.read" in reason
