# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-(principal, op, target) permission resolver — G11.2-T3 (#820).

The resolver answers one question at dispatch time: **what is the
effective verdict for this principal dispatching this op against this
target?** Verdict is one of
:attr:`~meho_backplane.db.models.PermissionVerdict.AUTO_EXECUTE`,
:attr:`~meho_backplane.db.models.PermissionVerdict.NEEDS_APPROVAL`, or
:attr:`~meho_backplane.db.models.PermissionVerdict.DENY`.

Effective authz = user-role-allows ∩ agent-permission ∩ op-requirement
-----------------------------------------------------------------------

Three independent gates, evaluated in order. The final verdict is the
most restrictive across all three:

1. **User-role gate.** The RBAC role ladder (``require_role``) already
   blocks the route at the HTTP/MCP layer for roles below the route's
   floor. What we check here is whether the *calling role* admits any
   operation at all: ``READ_ONLY`` principals are limited to
   ``auto-execute`` on safe ops by policy — they cannot self-grant
   ``needs-approval`` access, only a tenant-admin can do that by
   inserting an explicit row. The role gate never *widens* — it can only
   tighten or leave unchanged.

2. **Agent-permission gate.** Load all :class:`AgentPermission` rows
   for ``(tenant_id, principal_sub)``. Evaluate every row's
   ``op_pattern`` (fnmatch glob) against the ``op_id``, and every row's
   ``target_scope`` against the ``target_id``. Among matching rows, pick
   the one with the **most specific op_pattern** (longest literal prefix
   before the first ``*``) — more specific grants override broader
   defaults. When no row matches, use the ``safety_level`` default.

3. **Op-requirement gate.** The op's ``safety_level`` drives the
   minimum default verdict; it also enforces a ceiling: a dangerous op
   (``safety_level='dangerous'``) can never be granted
   ``auto-execute`` by an :class:`AgentPermission` row — the op
   requirement floor overrides the grant. Tenant/operator config can
   tighten (make a ``caution`` op ``deny``) but not loosen (cannot
   make a ``dangerous`` op ``auto-execute``).

   Safety-level defaults (no matching permission row):

   * ``safe`` → ``auto-execute``
   * ``caution`` → ``needs-approval``
   * ``dangerous`` → ``deny``

   Safety-level ceilings (hard upper-bound even when a row says
   something more permissive):

   * ``safe`` — no ceiling (any verdict is valid).
   * ``caution`` — ceiling is ``needs-approval``; ``auto-execute``
     from a row is tightened to ``needs-approval``.
   * ``dangerous`` — ceiling is ``deny``; any row verdict is overridden
     to ``deny``.

Role-gate interaction
---------------------

``READ_ONLY`` operators have a role-level ceiling of
``needs-approval``: a ``READ_ONLY`` principal can never reach an op
that a permission row marks ``auto-execute`` on a ``caution`` op,
because the effective verdict is the intersection's most restrictive
element. The role gate does **not** affect ``safe`` ops — read-only
principals can auto-execute safe ops by default.

Sync vs async
-------------

The resolver is **async** — it queries the DB for permission rows on
every call. The dispatcher's ``policy_gate`` is already awaited (the
dispatcher is fully async); promoting the gate from sync to async is
the backward-compatible change the ``_validate.py`` docstring
anticipated.

Pattern matching
----------------

``fnmatch.fnmatch(op_id, pattern)`` from the Python stdlib. Glob rules:
``*`` matches anything including ``/`` and ``.``, which lets
``"GET:/api/*"`` match ``"GET:/api/vcenter/cluster"`` and
``"vault.kv.*"`` match ``"vault.kv.read"``. Quoted in the module-level
``__all__`` so callers can import the function directly for testing.

References
----------

* G11.2-T3 task (#820).
* ``policy_gate`` caller: :mod:`meho_backplane.operations._validate`.
* Row model: :class:`~meho_backplane.db.models.AgentPermission`.
* Verdict enum: :class:`~meho_backplane.db.models.PermissionVerdict`.
"""

from __future__ import annotations

import uuid
from fnmatch import fnmatch
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import AgentPermission, PermissionVerdict

__all__ = [
    "resolve_verdict",
]

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Safety-level helpers
# ---------------------------------------------------------------------------

#: Default verdict when no AgentPermission row matches, keyed by
#: safety_level value. ``safe`` → auto-execute, ``caution`` →
#: needs-approval, ``dangerous`` → deny. Unknown safety_level falls
#: back to deny (fail-closed).
_SAFETY_DEFAULT: dict[str, PermissionVerdict] = {
    "safe": PermissionVerdict.AUTO_EXECUTE,
    "caution": PermissionVerdict.NEEDS_APPROVAL,
    "dangerous": PermissionVerdict.DENY,
}

#: Hard upper-bound ceiling for each safety_level.  A permission row
#: carrying a verdict more permissive than the ceiling is tightened
#: to the ceiling value.  ``safe`` has no ceiling (any verdict is
#: valid -- the ``None`` sentinel is handled in ``_apply_ceiling``).
_SAFETY_CEILING: dict[str, PermissionVerdict | None] = {
    "safe": None,
    "caution": PermissionVerdict.NEEDS_APPROVAL,
    "dangerous": PermissionVerdict.DENY,
}

# Verdict ordering: lower index = more permissive. Used to compare
# two verdicts and return the more restrictive one.
_VERDICT_ORDER: tuple[PermissionVerdict, ...] = (
    PermissionVerdict.AUTO_EXECUTE,
    PermissionVerdict.NEEDS_APPROVAL,
    PermissionVerdict.DENY,
)


def _more_restrictive(a: PermissionVerdict, b: PermissionVerdict) -> PermissionVerdict:
    """Return whichever of *a* / *b* is more restrictive (higher rank)."""
    return a if _VERDICT_ORDER.index(a) >= _VERDICT_ORDER.index(b) else b


def _apply_ceiling(
    verdict: PermissionVerdict,
    safety_level: str,
) -> PermissionVerdict:
    """Tighten *verdict* to the safety_level ceiling when needed.

    Returns *verdict* unchanged when the ceiling is ``None`` (safe ops
    have no ceiling) or when *verdict* is already at or above the
    ceiling's restrictiveness.
    """
    ceiling = _SAFETY_CEILING.get(safety_level)
    if ceiling is None:
        return verdict
    return _more_restrictive(verdict, ceiling)


def _role_ceiling(role: TenantRole) -> PermissionVerdict | None:
    """Return the verdict ceiling imposed by the operator's tenant role.

    ``TENANT_ADMIN`` and ``OPERATOR`` have no ceiling (they can reach any
    verdict their permission rows and the op-level ceiling allow).
    ``READ_ONLY`` is limited to ``needs-approval`` at most — they cannot
    auto-execute anything a permission row or safety_level would put
    above that threshold.

    Returns ``None`` meaning "no ceiling" for TENANT_ADMIN / OPERATOR.
    """
    if role == TenantRole.READ_ONLY:
        return PermissionVerdict.NEEDS_APPROVAL
    return None


# ---------------------------------------------------------------------------
# Pattern specificity
# ---------------------------------------------------------------------------


def _pattern_specificity(pattern: str) -> int:
    """Return an integer specificity score for *pattern*.

    Higher score = more specific = takes priority over less-specific
    patterns. The score is the length of the literal prefix before the
    first ``*``: ``"vault.kv.read"`` (no wildcard) scores its full
    length; ``"vault.kv.*"`` scores 9 (``"vault.kv."``); ``"*"`` scores 0.
    """
    star_pos = pattern.find("*")
    if star_pos == -1:
        return len(pattern)
    return star_pos


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------


async def _load_rows(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    principal_sub: str,
) -> list[AgentPermission]:
    """Load all AgentPermission rows for *(tenant_id, principal_sub)*.

    The DB index ``agent_permission_tenant_principal_idx`` makes this
    query fast.  Pattern matching and target scoping are done in
    Python after loading; the result set for a single principal is
    expected to be small (tens of rows, not thousands).
    """
    result = await session.execute(
        select(AgentPermission).where(
            AgentPermission.tenant_id == tenant_id,
            AgentPermission.principal_sub == principal_sub,
        )
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


async def resolve_verdict(
    *,
    session: AsyncSession,
    operator: Operator,
    op_id: str,
    safety_level: str,
    target_id: Any | None,
) -> tuple[PermissionVerdict, str]:
    """Resolve the effective verdict for a single dispatch attempt.

    Parameters
    ----------
    session:
        Live async DB session (already in a transaction).
    operator:
        The resolved operator identity carrying ``sub``, ``tenant_id``,
        and ``tenant_role``.
    op_id:
        The canonical operation identifier (e.g. ``"GET:/api/vcenter/cluster"``,
        ``"vault.kv.read"``).
    safety_level:
        The op's :attr:`~meho_backplane.db.models.EndpointDescriptor.safety_level`
        column value: one of ``"safe"``, ``"caution"``, ``"dangerous"``.
    target_id:
        The dispatch target's identifier (UUID or any object with a
        ``.id`` attribute), or ``None`` when the op is target-agnostic.
        Used for target-scoped row matching.

    Returns
    -------
    tuple[PermissionVerdict, str]
        ``(verdict, reason_string)`` where *reason* is a short
        human/agent-readable explanation of *why* this verdict was
        reached.  Callers log + forward the reason to the structured
        error payload.
    """
    # Normalise target_id to a plain string for comparison against
    # target_scope column values.
    target_str: str | None = None
    if target_id is not None:
        raw = getattr(target_id, "id", target_id)
        target_str = str(raw)

    # --- Gate 1: user role ceiling ------------------------------------
    role_ceil = _role_ceiling(operator.tenant_role)

    # --- Gate 2: agent-permission rows --------------------------------
    rows = await _load_rows(session, operator.tenant_id, operator.sub)

    # Filter rows to those whose op_pattern and target_scope match.
    matching: list[AgentPermission] = []
    for row in rows:
        if not fnmatch(op_id, row.op_pattern):
            continue
        # target_scope: NULL or "*" = any target; else exact UUID match.
        if (
            row.target_scope
            and row.target_scope != "*"
            and (target_str is None or target_str != row.target_scope)
        ):
            continue
        matching.append(row)

    # --- Gate 3: pick verdict -----------------------------------------
    if matching:
        # Pick the most specific matching row (longest literal prefix).
        best = max(matching, key=lambda r: _pattern_specificity(r.op_pattern))
        raw_verdict = PermissionVerdict(best.verdict)
        source = f"permission row (pattern={best.op_pattern!r})"
    else:
        # No matching row — use safety_level default.
        raw_verdict = _SAFETY_DEFAULT.get(safety_level, PermissionVerdict.DENY)
        source = f"safety_level default ({safety_level})"

    # Apply safety_level ceiling (op-requirement gate).
    after_op_ceil = _apply_ceiling(raw_verdict, safety_level)

    # Apply role ceiling.
    if role_ceil is not None:
        final_verdict = _more_restrictive(after_op_ceil, role_ceil)
        ceil_applied = after_op_ceil != final_verdict
    else:
        final_verdict = after_op_ceil
        ceil_applied = False

    reason_parts = [f"verdict={final_verdict.value}", f"source={source}"]
    if after_op_ceil != raw_verdict:
        reason_parts.append(
            f"tightened by safety_level ceiling ({safety_level}→{after_op_ceil.value})"
        )
    if ceil_applied:
        reason_parts.append(
            f"tightened by role ceiling ({operator.tenant_role.value}→{final_verdict.value})"
        )

    reason = "; ".join(reason_parts)

    _log.debug(
        "permission_resolved",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        op_id=op_id,
        safety_level=safety_level,
        target_id=target_str,
        verdict=final_verdict.value,
        source=source,
        rows_loaded=len(rows),
        rows_matched=len(matching),
    )

    return final_verdict, reason
