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
  (``tenant_id IS NULL``) applies to the whole install. Each scope's
  override is stored on its own, against the classification: a tenant's
  choice stays when an install-wide override that said the same goes away.
* **Removal.** :func:`remove_override` deletes one scope's override. A
  platform admin can remove any tenant's; a tenant admin only their own.
  Ending approval this way passes the same checks as turning it off.
* **Reach.** An install-wide override also applies to a tenant's own copy
  of the operation (a tenant row with the same name), whose
  classification may differ from the built-in one. Before an install-wide
  override is lowered or removed, every tenant it reaches is checked: when
  approval ends for any of them, the approval-off checks apply, and the
  audit row names those tenants. An install-wide "require approval" is
  kept when any tenant copy needs it.

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
    "OverrideNotFoundError",
    "PolicyEditResult",
    "apply_effective_policy",
    "apply_policy_edit",
    "effective_policy",
    "effective_policy_map",
    "list_overrides",
    "load_overrides",
    "remove_override",
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


class OverrideNotFoundError(LookupError):
    """There is no override of the operation in the asked scope."""

    def __init__(self, op_id: str, tenant_id: UUID | None) -> None:
        scope = "the whole install" if tenant_id is None else f"tenant {tenant_id}"
        super().__init__(f"{op_id!r} has no override for {scope}.")


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
    """What an edit changed, for the audit row.

    ``approval_ended_for_tenants`` names the tenants whose own copy of the
    operation lost its approval requirement through an install-wide
    change, while the built-in operation kept it.
    """

    changes: dict[str, dict[str, Any]]
    scope: Literal["tenant", "install"]
    approval_ended_for_tenants: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class _TenantScope:
    """One tenant that an install-wide override reaches, with that tenant's values."""

    tenant_id: UUID
    code_safety: str
    code_approval: bool
    own: list[OperationPolicyOverride]


def _key_clause(model: type[EndpointDescriptor] | type[OperationPolicyOverride], key: OpKey) -> Any:
    return and_(
        model.product == key.product,
        model.version == key.version,
        model.impl_id == key.impl_id,
        model.op_id == key.op_id,
    )


async def _tenant_scopes(
    session: AsyncSession, key: OpKey, *, code_safety: str, code_approval: bool
) -> list[_TenantScope]:
    """Return every tenant an install-wide override of *key* reaches.

    A tenant with its own copy of the operation keeps that copy's
    classification. A tenant with only its own override keeps the
    built-in one (*code_safety*, *code_approval*).
    """
    copies: dict[UUID, EndpointDescriptor] = {}
    for row in (
        await session.execute(
            select(EndpointDescriptor).where(
                _key_clause(EndpointDescriptor, key), EndpointDescriptor.tenant_id.is_not(None)
            )
        )
    ).scalars():
        if row.tenant_id is not None:
            copies[row.tenant_id] = row
    own: dict[UUID, list[OperationPolicyOverride]] = {}
    for override in (
        await session.execute(
            select(OperationPolicyOverride).where(
                _key_clause(OperationPolicyOverride, key),
                OperationPolicyOverride.tenant_id.is_not(None),
            )
        )
    ).scalars():
        if override.tenant_id is not None:
            own.setdefault(override.tenant_id, []).append(override)
    scopes: list[_TenantScope] = []
    for tenant_id in sorted(copies.keys() | own.keys()):
        copy = copies.get(tenant_id)
        scopes.append(
            _TenantScope(
                tenant_id=tenant_id,
                code_safety=copy.safety_level if copy is not None else code_safety,
                code_approval=copy.requires_approval if copy is not None else code_approval,
                own=own.get(tenant_id, []),
            )
        )
    return scopes


def _tenants_losing_approval(
    scopes: Sequence[_TenantScope],
    *,
    before: OperationPolicyOverride | None,
    after: OperationPolicyOverride | None,
) -> tuple[UUID, ...]:
    """The tenants in *scopes* where approval ends when *before* becomes *after*."""
    lost: list[UUID] = []
    for scope in scopes:
        old = effective_policy(scope.code_safety, scope.code_approval, [before, *scope.own])
        new = effective_policy(scope.code_safety, scope.code_approval, [after, *scope.own])
        if old.requires_approval and not new.requires_approval:
            lost.append(scope.tenant_id)
    return tuple(lost)


def _refuse_unless_may_end_approval(
    op_id: str,
    *,
    old: EffectivePolicy,
    new: EffectivePolicy,
    lost: Sequence[UUID],
    who: _Requester,
) -> None:
    """Run the approval-off checks when approval ends in any scope the edit reaches.

    That is the edited scope itself (*old* to *new*) or a tenant in
    *lost*. A level of ``destructive`` always keeps approval, so when
    approval ends anywhere, *new* is not destructive.
    """
    if not (old.requires_approval and not new.requires_approval) and not lost:
        return
    if who.tighten_only:
        raise _edit_tool_cannot_loosen()
    _check_may_turn_approval_off(op_id, new.safety_level, who)


def _split(
    overrides: list[OperationPolicyOverride], scope_tenant_id: UUID | None
) -> tuple[OperationPolicyOverride | None, list[OperationPolicyOverride]]:
    mine = next((o for o in overrides if o.tenant_id == scope_tenant_id), None)
    return mine, [o for o in overrides if o is not mine]


def _new_safety_field(
    *,
    row: EndpointDescriptor,
    old: EffectivePolicy,
    requested: str,
    tighten_only: bool,
) -> str | None:
    """Check a requested risk level; return the value to store (``None`` = no override).

    The stored value is measured against the classification only, not
    against the other scopes' overrides: a tenant's choice must still hold
    when an install-wide override that said the same is removed.
    """
    if SAFETY_RANK[requested] < SAFETY_RANK[row.safety_level]:
        raise OpPolicyEditError(
            "risk_level_below_classification",
            f"safety_level {requested!r} is lower than the classification of "
            f"{row.op_id!r} ({row.safety_level!r}). {_PULL_REQUEST_HINT}",
        )
    if tighten_only and SAFETY_RANK[requested] < SAFETY_RANK[old.safety_level]:
        raise _edit_tool_cannot_loosen()
    return None if SAFETY_RANK[requested] <= SAFETY_RANK[row.safety_level] else requested


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
    tenants: Sequence[_TenantScope],
    requested: bool,
    new_safety: str,
    who: _Requester,
) -> bool | None:
    """Check a requested approval flag; return the value to store (``None`` = no override).

    ``True`` is stored unless the classification requires approval, no
    other scope's override turns it off, and every tenant in *tenants*
    requires it on its own: by its copy's classification or by its own
    override. Like the risk level, the choice is kept apart from the
    other scopes' overrides.
    """
    base = _approval(row.requires_approval, others)
    if requested:
        turned_off = any(o.requires_approval is False for o in others)
        needed_by_a_tenant = any(
            not effective_policy(t.code_safety, t.code_approval, t.own).requires_approval
            for t in tenants
        )
        return None if row.requires_approval and not turned_off and not needed_by_a_tenant else True
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
    tenants: list[_TenantScope] = []
    if scope_tenant_id is None:
        tenants = await _tenant_scopes(
            session, key, code_safety=row.safety_level, code_approval=row.requires_approval
        )

    safety_field = mine.safety_level if mine is not None else None
    if safety_level is not None:
        safety_field = _new_safety_field(
            row=row, old=old, requested=safety_level, tighten_only=tighten_only
        )
    new_safety = effective_policy(
        row.safety_level, row.requires_approval, [*others, _probe(safety_field)]
    ).safety_level
    approval_field = mine.requires_approval if mine is not None else None
    if requires_approval is not None:
        approval_field = _new_approval_field(
            row=row,
            others=others,
            tenants=tenants,
            requested=requires_approval,
            new_safety=new_safety,
            who=who,
        )
    candidate = OperationPolicyOverride(safety_level=safety_field, requires_approval=approval_field)
    new = effective_policy(row.safety_level, row.requires_approval, [*others, candidate])
    # Every edit that ends approval passes the same checks, also one that
    # only removes an override or lowers a raised risk level, and one that
    # ends approval for a tenant's own copy of a built-in operation.
    lost = _tenants_losing_approval(tenants, before=mine, after=candidate)
    _refuse_unless_may_end_approval(row.op_id, old=old, new=new, lost=lost, who=who)
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
    return PolicyEditResult(
        changes=_changes(
            old,
            new,
            safety_touched=safety_level is not None,
            approval_touched=requires_approval is not None,
        ),
        scope=_scope_label(scope_tenant_id),
        approval_ended_for_tenants=lost,
    )


def _scope_label(scope_tenant_id: UUID | None) -> Literal["tenant", "install"]:
    return "install" if scope_tenant_id is None else "tenant"


def _changes(
    old: EffectivePolicy, new: EffectivePolicy, *, safety_touched: bool, approval_touched: bool
) -> dict[str, dict[str, Any]]:
    """The old and new effective values for the audit row."""
    changes: dict[str, dict[str, Any]] = {}
    if safety_touched or new.safety_level != old.safety_level:
        changes["safety_level"] = {"old": old.safety_level, "new": new.safety_level}
    if approval_touched or new.requires_approval != old.requires_approval:
        changes["requires_approval"] = {"old": old.requires_approval, "new": new.requires_approval}
    return changes


async def remove_override(
    session: AsyncSession,
    *,
    key: OpKey,
    scope_tenant_id: UUID | None,
    operator: Operator,
    reason: str | None,
    person_present: bool,
    max_age_seconds: int,
) -> PolicyEditResult:
    """Delete the override of one scope; return what changed there.

    *scope_tenant_id* names the override: a tenant UUID or ``None`` for
    the install-wide one. The caller checks that *operator* may act on
    that scope. When the removal ends approval in that scope, or for a
    tenant that an install-wide override reaches, it passes the same
    checks as turning approval off. Raises :class:`OverrideNotFoundError`
    when the scope has no override.
    """
    overrides = (await load_overrides(session, tenant_id=scope_tenant_id, keys=[key])).get(key, [])
    mine, others = _split(overrides, scope_tenant_id)
    if mine is None:
        raise OverrideNotFoundError(key.op_id, scope_tenant_id)
    code_safety, code_approval = await _classification(session, key, scope_tenant_id)
    old = effective_policy(code_safety, code_approval, overrides)
    new = effective_policy(code_safety, code_approval, others)
    tenants: list[_TenantScope] = []
    if scope_tenant_id is None:
        tenants = await _tenant_scopes(
            session, key, code_safety=code_safety, code_approval=code_approval
        )
    lost = _tenants_losing_approval(tenants, before=mine, after=None)
    who = _Requester(operator, reason, person_present, False, max_age_seconds)
    _refuse_unless_may_end_approval(key.op_id, old=old, new=new, lost=lost, who=who)
    await session.delete(mine)
    await session.flush()
    return PolicyEditResult(
        changes=_changes(
            old,
            new,
            safety_touched=mine.safety_level is not None,
            approval_touched=mine.requires_approval is not None,
        ),
        scope=_scope_label(scope_tenant_id),
        approval_ended_for_tenants=lost,
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
    """Return the effective policy of *row*'s operation in *row*'s scope."""
    key = OpKey(row.product, row.version, row.impl_id, row.op_id)
    code_safety, code_approval = await _classification(session, key, row.tenant_id)
    overrides = (await load_overrides(session, tenant_id=row.tenant_id, keys=[key])).get(key, [])
    return effective_policy(code_safety, code_approval, overrides)


async def _classification(
    session: AsyncSession, key: OpKey, tenant_id: UUID | None
) -> tuple[str, bool]:
    """Return the classification of *key* as seen from *tenant_id*'s scope.

    It comes from the scope's own descriptor row when the tenant has one,
    else from the built-in row. With no descriptor (the operation is gone)
    the lowest values apply, so only the overrides count.
    """
    descriptors = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    _key_clause(EndpointDescriptor, key),
                    or_(
                        EndpointDescriptor.tenant_id.is_(None),
                        EndpointDescriptor.tenant_id == tenant_id,
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
    if descriptor is None:
        return "safe", False
    return descriptor.safety_level, descriptor.requires_approval
