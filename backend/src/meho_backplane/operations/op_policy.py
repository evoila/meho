# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator overrides of an operation's risk level and approval flag (#4711).

The value in code (typed operations) or in the API description (ingested
operations) stays on :class:`~meho_backplane.db.models.EndpointDescriptor`.
That value is the *classification*. An operator's choice is kept apart, in
:class:`~meho_backplane.db.models.OperationPolicyOverride`, so a start or a
new ingest never overwrites it.

The rules:

* **Risk level.** The effective level is the highest of the
  classification and every override that applies. An edit can never set a
  level below the classification; that needs a pull request.
* **Approval.** Approval is required when the classification says so or
  an override requires it. The one way to turn it off is an install-wide
  override with ``requires_approval=False``, set by a platform admin with a
  reason and a fresh sign-in. Every edit that ends approval (also removing
  an override) passes these checks. A destructive operation always waits
  for approval.
* **Scope.** A tenant admin's override (``tenant_id`` = the tenant) applies
  to that tenant only. A platform admin's override on a built-in operation
  (``tenant_id IS NULL``) applies to the whole install.

Dispatch, preview, search and the operation contract all read the
effective values through :func:`apply_effective_policy` /
:func:`effective_policy_map`, so they always agree.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride

__all__ = [
    "SAFETY_RANK",
    "EffectivePolicy",
    "OpKey",
    "OpPolicyEditError",
    "PolicyEditResult",
    "apply_effective_policy",
    "apply_policy_edit",
    "effective_policy",
    "effective_policy_map",
    "list_overrides",
    "load_overrides",
]

#: Order of the four risk levels, lowest first.
SAFETY_RANK: Final[dict[str, int]] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}

#: What the edit tools say when a risk level would go below the classification.
_PULL_REQUEST_HINT: Final[str] = (
    "An edit can only raise the risk level. To lower the classification, "
    "open a pull request that changes it in the code."
)

PolicyEditErrorCode = Literal[
    "risk_level_below_classification",
    "edit_tool_cannot_loosen",
    "approval_off_destructive",
    "approval_off_needs_platform_admin",
    "approval_off_needs_reason",
    "approval_off_needs_fresh_sign_in",
    "approval_required_for_install",
]


class OpPolicyEditError(ValueError):
    """A risk level or approval edit that the rules refuse.

    A :class:`ValueError` so every edit surface that already maps
    ``ValueError`` to a caller error (REST ``400``, MCP ``-32602``, console
    panel) keeps working. ``code`` lets REST pick ``403`` / ``401`` for the
    two cases that are about who is asking, not about the value.
    """

    def __init__(self, code: PolicyEditErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: PolicyEditErrorCode = code


@dataclass(frozen=True)
class OpKey:
    """The name of one operation, the key overrides are stored under."""

    product: str
    version: str
    impl_id: str
    op_id: str

    @classmethod
    def of(cls, row: EndpointDescriptor) -> OpKey:
        """Return the key of a descriptor row."""
        return cls(row.product, row.version, row.impl_id, row.op_id)


@dataclass(frozen=True)
class EffectivePolicy:
    """The risk level and approval flag that apply after overrides."""

    safety_level: str
    requires_approval: bool


def _approval(code_approval: bool, overrides: Iterable[OperationPolicyOverride]) -> bool:
    """Return the approval flag from the classification and *overrides*.

    Any override that requires approval wins. An override that turned
    approval off removes the classification's requirement. The destructive
    rule is applied by the caller.
    """
    flags = [o.requires_approval for o in overrides if o.requires_approval is not None]
    if True in flags:
        return True
    return code_approval and False not in flags


def _max_level(levels: Iterable[str]) -> str:
    return max(levels, key=lambda level: SAFETY_RANK.get(level, 0))


def effective_policy(
    code_safety: str,
    code_approval: bool,
    overrides: Iterable[OperationPolicyOverride | None],
) -> EffectivePolicy:
    """Return the stricter of the classification and *overrides*."""
    present = [o for o in overrides if o is not None]
    safety = _max_level([code_safety, *(o.safety_level for o in present if o.safety_level)])
    approval = _approval(code_approval, present) or safety == "destructive"
    return EffectivePolicy(safety_level=safety, requires_approval=approval)


def _visible_scope(tenant_id: UUID | None) -> Any:
    """Overrides that apply to *tenant_id*: the install-wide ones plus its own."""
    install = OperationPolicyOverride.tenant_id.is_(None)
    if tenant_id is None:
        return install
    return or_(install, OperationPolicyOverride.tenant_id == tenant_id)


async def load_overrides(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    keys: Iterable[OpKey],
) -> dict[OpKey, list[OperationPolicyOverride]]:
    """Load the overrides that apply to *tenant_id* for *keys*.

    One query, filtered by the connectors the keys belong to. The table
    holds one row per operator choice, so it stays small.
    """
    wanted = set(keys)
    if not wanted:
        return {}
    triples = {(k.product, k.version, k.impl_id) for k in wanted}
    stmt = select(OperationPolicyOverride).where(
        _visible_scope(tenant_id),
        or_(
            *(
                and_(
                    OperationPolicyOverride.product == product,
                    OperationPolicyOverride.version == version,
                    OperationPolicyOverride.impl_id == impl_id,
                )
                for product, version, impl_id in triples
            )
        ),
    )
    found: dict[OpKey, list[OperationPolicyOverride]] = {}
    for row in (await session.execute(stmt)).scalars().all():
        key = OpKey(row.product, row.version, row.impl_id, row.op_id)
        if key in wanted:
            found.setdefault(key, []).append(row)
    return found


async def effective_policy_map(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    rows: Sequence[EndpointDescriptor],
) -> dict[OpKey, EffectivePolicy]:
    """Return the effective policy of every row in *rows* for *tenant_id*."""
    overrides = await load_overrides(session, tenant_id=tenant_id, keys=map(OpKey.of, rows))
    return {
        OpKey.of(row): effective_policy(
            row.safety_level, row.requires_approval, overrides.get(OpKey.of(row), [])
        )
        for row in rows
    }


async def apply_effective_policy(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    row: EndpointDescriptor,
) -> EndpointDescriptor:
    """Detach *row* from *session* and set its effective risk level and approval.

    The row is expunged first, so the changed values are never written
    back: the stored row keeps the classification.
    """
    policy = (await effective_policy_map(session, tenant_id=tenant_id, rows=[row]))[OpKey.of(row)]
    session.expunge(row)
    row.safety_level = policy.safety_level
    row.requires_approval = policy.requires_approval
    return row


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyEditResult:
    """What an edit changed, for the audit row."""

    changes: dict[str, dict[str, Any]]
    scope: Literal["tenant", "install"]


def _split(
    overrides: list[OperationPolicyOverride], scope_tenant_id: UUID | None
) -> tuple[OperationPolicyOverride | None, list[OperationPolicyOverride]]:
    mine = next((o for o in overrides if o.tenant_id == scope_tenant_id), None)
    return mine, [o for o in overrides if o is not mine]


def _new_safety_field(
    *,
    row: EndpointDescriptor,
    others: list[OperationPolicyOverride],
    old: EffectivePolicy,
    requested: str,
    tighten_only: bool,
) -> str | None:
    """Check a requested risk level; return the value to store (``None`` = no override)."""
    if SAFETY_RANK[requested] < SAFETY_RANK[row.safety_level]:
        raise OpPolicyEditError(
            "risk_level_below_classification",
            f"safety_level {requested!r} is lower than the classification of "
            f"{row.op_id!r} ({row.safety_level!r}). {_PULL_REQUEST_HINT}",
        )
    if tighten_only and SAFETY_RANK[requested] < SAFETY_RANK[old.safety_level]:
        raise _edit_tool_cannot_loosen()
    base = effective_policy(row.safety_level, row.requires_approval, others).safety_level
    return None if SAFETY_RANK[requested] <= SAFETY_RANK[base] else requested


def _edit_tool_cannot_loosen() -> OpPolicyEditError:
    return OpPolicyEditError(
        "edit_tool_cannot_loosen",
        "meho_connector_edit_op can only make an operation stricter. It cannot "
        "turn approval off or lower a risk level. To turn approval off, a "
        "platform admin uses the console or the CLI (`meho connector edit-op "
        "--no-requires-approval --reason ...`). To lower a risk level, open a "
        "pull request that changes the classification in the code.",
    )


@dataclass(frozen=True)
class _Requester:
    """Who asks for the edit, and how."""

    operator: Operator
    reason: str | None
    person_present: bool
    tighten_only: bool
    max_age_seconds: int


def _new_approval_field(
    *,
    row: EndpointDescriptor,
    others: list[OperationPolicyOverride],
    requested: bool,
    new_safety: str,
    who: _Requester,
) -> bool | None:
    """Check a requested approval flag; return the value to store (``None`` = no override)."""
    base = _approval(row.requires_approval, others)
    if requested:
        return None if base else True
    if who.tighten_only:
        raise _edit_tool_cannot_loosen()
    if not base:
        return None
    if not row.requires_approval:
        raise OpPolicyEditError(
            "approval_required_for_install",
            f"approval for {row.op_id!r} is required by a setting for the whole "
            "install. Only a platform admin can remove that setting.",
        )
    _check_may_turn_approval_off(row.op_id, new_safety, who)
    return False


def _check_may_turn_approval_off(op_id: str, new_safety: str, who: _Requester) -> None:
    """Refuse turning approval off unless every condition of the rule holds."""
    if new_safety == "destructive":
        raise OpPolicyEditError(
            "approval_off_destructive",
            f"approval cannot be turned off for {op_id!r}: it is a destructive "
            "operation, and a destructive operation always waits for approval.",
        )
    if not who.operator.platform_admin:
        raise OpPolicyEditError(
            "approval_off_needs_platform_admin",
            f"only a platform admin can turn approval off for {op_id!r}. A tenant "
            "admin can only make an operation stricter.",
        )
    if not (who.reason and who.reason.strip()):
        raise OpPolicyEditError(
            "approval_off_needs_reason",
            f"turning approval off for {op_id!r} needs a reason. Pass a non-empty `reason`.",
        )
    if not who.person_present:
        minutes = max(who.max_age_seconds // 60, 1)
        raise OpPolicyEditError(
            "approval_off_needs_fresh_sign_in",
            "turning approval off needs a person with a fresh sign-in. Sign in "
            f"again (`meho login`) and retry within {minutes} minutes, or use the "
            "console. An API token, a service principal or an agent cannot do it.",
        )


async def apply_policy_edit(
    session: AsyncSession,
    *,
    row: EndpointDescriptor,
    scope_tenant_id: UUID | None,
    operator: Operator,
    safety_level: str | None,
    requires_approval: bool | None,
    reason: str | None,
    person_present: bool,
    tighten_only: bool,
    max_age_seconds: int,
) -> PolicyEditResult:
    """Check one risk level / approval edit and store it as an override.

    *row* holds the classification; it is never changed. *scope_tenant_id*
    is where the override is stored: a tenant UUID (that tenant only) or
    ``None`` (the whole install). Raises :class:`OpPolicyEditError` when a
    rule refuses the edit; nothing is written then.
    """
    key = OpKey.of(row)
    overrides = (await load_overrides(session, tenant_id=scope_tenant_id, keys=[key])).get(key, [])
    mine, others = _split(overrides, scope_tenant_id)
    old = effective_policy(row.safety_level, row.requires_approval, overrides)
    who = _Requester(operator, reason, person_present, tighten_only, max_age_seconds)

    safety_field = mine.safety_level if mine is not None else None
    if safety_level is not None:
        safety_field = _new_safety_field(
            row=row, others=others, old=old, requested=safety_level, tighten_only=tighten_only
        )
    new_safety = effective_policy(
        row.safety_level, row.requires_approval, [*others, _probe(safety_field)]
    ).safety_level
    approval_field = mine.requires_approval if mine is not None else None
    if requires_approval is not None:
        approval_field = _new_approval_field(
            row=row,
            others=others,
            requested=requires_approval,
            new_safety=new_safety,
            who=who,
        )
    candidate = OperationPolicyOverride(safety_level=safety_field, requires_approval=approval_field)
    new = effective_policy(row.safety_level, row.requires_approval, [*others, candidate])
    if old.requires_approval and not new.requires_approval:
        # Every edit that ends approval passes the same checks, also one
        # that only removes an override or lowers a raised risk level.
        if tighten_only:
            raise _edit_tool_cannot_loosen()
        _check_may_turn_approval_off(row.op_id, new.safety_level, who)
    await _store(
        session,
        key=key,
        mine=mine,
        scope_tenant_id=scope_tenant_id,
        safety_field=safety_field,
        approval_field=approval_field,
        reason=reason,
        operator=operator,
        approval_edited=requires_approval is not None,
    )
    changes: dict[str, dict[str, Any]] = {}
    if safety_level is not None or new.safety_level != old.safety_level:
        changes["safety_level"] = {"old": old.safety_level, "new": new.safety_level}
    if requires_approval is not None or new.requires_approval != old.requires_approval:
        changes["requires_approval"] = {"old": old.requires_approval, "new": new.requires_approval}
    return PolicyEditResult(
        changes=changes, scope="install" if scope_tenant_id is None else "tenant"
    )


def _probe(safety_field: str | None) -> OperationPolicyOverride:
    """A throw-away override carrying only *safety_field*, for computing a level."""
    return OperationPolicyOverride(safety_level=safety_field, requires_approval=None)


async def _store(
    session: AsyncSession,
    *,
    key: OpKey,
    mine: OperationPolicyOverride | None,
    scope_tenant_id: UUID | None,
    safety_field: str | None,
    approval_field: bool | None,
    reason: str | None,
    operator: Operator,
    approval_edited: bool,
) -> None:
    """Write, update or delete the override of this scope."""
    if safety_field is None and approval_field is None:
        if mine is not None:
            await session.delete(mine)
        return
    if mine is None:
        mine = OperationPolicyOverride(
            tenant_id=scope_tenant_id,
            product=key.product,
            version=key.version,
            impl_id=key.impl_id,
            op_id=key.op_id,
        )
        session.add(mine)
    elif not approval_edited and approval_field is False and mine.requires_approval is False:
        # This edit did not touch the approval exception. Keep who turned
        # approval off, when, and why; the audit row records this edit.
        mine.safety_level = safety_field
        await session.flush()
        return
    mine.safety_level = safety_field
    mine.requires_approval = approval_field
    mine.reason = reason.strip() if reason and reason.strip() else None
    mine.set_by_sub = operator.sub
    mine.set_at = datetime.now(UTC)
    await session.flush()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


async def list_overrides(
    session: AsyncSession,
    *,
    operator: Operator,
    reduced_approval_only: bool,
) -> list[tuple[OperationPolicyOverride, EffectivePolicy]]:
    """Return the overrides *operator* may see, each with its effective policy.

    A tenant admin sees the install-wide overrides and their own tenant's.
    A platform admin sees every tenant's too. The effective policy of a row
    is what applies in the row's scope: the classification plus every
    override visible there. ``reduced_approval_only`` keeps only the
    overrides that turned approval off and where approval is really off.
    """
    stmt = select(OperationPolicyOverride)
    if not operator.platform_admin:
        stmt = stmt.where(_visible_scope(operator.tenant_id))
    if reduced_approval_only:
        stmt = stmt.where(OperationPolicyOverride.requires_approval.is_(False))
    stmt = stmt.order_by(
        OperationPolicyOverride.impl_id,
        OperationPolicyOverride.version,
        OperationPolicyOverride.op_id,
        OperationPolicyOverride.set_at,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    report = [(row, await _scope_policy(session, row)) for row in rows]
    if reduced_approval_only:
        report = [(row, policy) for row, policy in report if not policy.requires_approval]
    return report


async def _scope_policy(session: AsyncSession, row: OperationPolicyOverride) -> EffectivePolicy:
    """Return the effective policy of *row*'s operation in *row*'s scope.

    The classification comes from the scope's own descriptor row when the
    tenant has one, else from the built-in row. With no descriptor (the
    operation is gone) only the overrides count.
    """
    key = OpKey(row.product, row.version, row.impl_id, row.op_id)
    descriptors = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == key.product,
                    EndpointDescriptor.version == key.version,
                    EndpointDescriptor.impl_id == key.impl_id,
                    EndpointDescriptor.op_id == key.op_id,
                    or_(
                        EndpointDescriptor.tenant_id.is_(None),
                        EndpointDescriptor.tenant_id == row.tenant_id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    descriptor = next(
        (d for d in descriptors if d.tenant_id is not None),
        next(iter(descriptors), None),
    )
    overrides = (await load_overrides(session, tenant_id=row.tenant_id, keys=[key])).get(key, [])
    if descriptor is None:
        return effective_policy("safe", False, overrides)
    return effective_policy(descriptor.safety_level, descriptor.requires_approval, overrides)
