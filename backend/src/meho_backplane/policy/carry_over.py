# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dry run of the "always wait" carry-over: list, change nothing.

"Always wait" is an admin's setting that makes an operation wait for
approval on every call. Two places hold it:

* an override with approval on (:class:`OperationPolicyOverride` with
  ``requires_approval`` true), for one operation;
* an entry of the policy file with ``always_wait: true``
  (:class:`TenantPolicy`), for a connector, an operation group or an
  operation, matched by pattern.

With the ``CUSTOM_POLICY`` switch on, MEHO will read each such setting as
a raise to ``destructive`` (the carry-over). :func:`carry_over_dry_run`
lists every saved setting of one scope and what the carry-over means for
it. It only reads: it changes no row and no answer of the gate, so it
reads no switch.

Per setting it says:

* where the setting comes from (``override`` or ``policy_file``) and its
  scope (``tenant`` or ``install``);
* the operations it makes wait today, and for each one the multi-step
  operations that use it as a step
  (:mod:`~meho_backplane.operations.governed_subops`). A ``destructive``
  step of a multi-step operation is refused, not held for approval;
* whether the install is in sandbox mode. There a delete grant may cover
  a ``destructive`` operation for a service login;
* that a ``destructive`` call needs a matching preview first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationGroup,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.operations.governed_subops import registered_governed_subop_surfaces
from meho_backplane.policy.classification import (
    ClassificationEntry,
    OpCoordinates,
    Scope,
    entries_from_document,
)
from meho_backplane.policy.store import InvalidStoredPolicyError
from meho_backplane.sandbox_mode import sandbox_mode_active

__all__ = [
    "DRY_RUN_MESSAGE",
    "CarryOverDryRun",
    "CarryOverOperation",
    "CarryOverSetting",
    "carry_over_dry_run",
]

#: The last line of every dry run.
DRY_RUN_MESSAGE: Final[str] = "Dry run: nothing was changed."

_PREVIEW_TEXT: Final[str] = (
    "As `destructive`, a call needs a matching preview first. A call without one is refused."
)
_SANDBOX_TEXT: Final[str] = (
    "This install is in sandbox mode. There a delete grant can cover a `destructive` "
    "operation for a service login. Today no grant covers an operation that always waits."
)
_NO_OPERATION_TEXT: Final[str] = "Today this entry makes no operation wait."


def _step_text(op_id: str, composites: Sequence[str]) -> str:
    return (
        f"`{op_id}` is a step of {', '.join(f'`{c}`' for c in composites)}. "
        "A `destructive` step of a multi-step operation is refused, not held for "
        "approval, so such a run stops at this step."
    )


class CarryOverOperation(BaseModel):
    """One operation a saved setting makes wait today.

    ``step_of`` lists the multi-step operations that use it as a step
    (empty when none does).
    """

    model_config = ConfigDict(frozen=True)

    connector_id: str
    op_id: str
    step_of: list[str]


class CarryOverSetting(BaseModel):
    """One saved "always wait" and what the carry-over to ``destructive`` means for it.

    ``source`` is ``override`` (an admin's approval on one operation) or
    ``policy_file`` (an entry with ``always_wait: true``). ``ref`` names
    the place: ``<scope>:override`` or ``<scope>:classifications[<index>]``.
    ``match`` is what the setting matches (``connector``, ``group``,
    ``op``). ``warnings`` say what changes with the carry-over.
    """

    model_config = ConfigDict(frozen=True)

    source: Literal["override", "policy_file"]
    scope: Scope
    ref: str
    match: dict[str, str]
    operations: list[CarryOverOperation]
    warnings: list[str]


class CarryOverDryRun(BaseModel):
    """The answer of the dry run: every saved "always wait" of ``scope``. Nothing is changed."""

    model_config = ConfigDict(frozen=True)

    scope: Scope
    sandbox_mode: bool
    settings: list[CarryOverSetting]
    message: str = DRY_RUN_MESSAGE


def _scope_filter(
    column: InstrumentedAttribute[UUID | None], scope: Scope, tenant_id: UUID
) -> ColumnElement[bool]:
    return column.is_(None) if scope == "install" else column == tenant_id


async def _operations(session: AsyncSession, scope: Scope, tenant_id: UUID) -> list[OpCoordinates]:
    """The operations a setting of *scope* can match, each once.

    A tenant's settings reach the built-in operations and the tenant's
    own. The install's settings reach every operation.
    """
    group = aliased(OperationGroup)
    stmt = select(
        EndpointDescriptor.impl_id,
        EndpointDescriptor.version,
        group.group_key,
        EndpointDescriptor.op_id,
    ).outerjoin(group, EndpointDescriptor.group_id == group.id)
    if scope == "tenant":
        stmt = stmt.where(
            or_(EndpointDescriptor.tenant_id.is_(None), EndpointDescriptor.tenant_id == tenant_id)
        )
    found = {
        OpCoordinates(f"{impl_id}-{version}", group_key, op_id)
        for impl_id, version, group_key, op_id in (await session.execute(stmt)).tuples()
    }
    return sorted(found, key=lambda c: (c.connector_id, c.op_id, c.group_key or ""))


def _steps_by_op() -> dict[str, list[str]]:
    """Map each step's op id to the multi-step operations that use it, sorted."""
    steps: dict[str, list[str]] = {}
    for composite, surface in registered_governed_subop_surfaces().items():
        for step in surface.sub_op_ids:
            steps.setdefault(step, []).append(composite)
    return {step: sorted(composites) for step, composites in steps.items()}


def _waiting_entries(
    entries: Sequence[ClassificationEntry], coords: OpCoordinates
) -> list[ClassificationEntry]:
    """The entries of one scope that make *coords* wait.

    The most specific entries that set ``always_wait`` decide it; on a tie
    ``true`` wins. So an entry with ``true`` that a more specific
    ``always_wait: false`` overrules makes this operation not wait.
    """
    setting = [e for e in entries if e.always_wait is not None and e.matches(coords)]
    if not setting:
        return []
    best = max(e.rank for e in setting)
    return [e for e in setting if e.rank == best and e.always_wait]


def _operation(coords: OpCoordinates, steps: Mapping[str, list[str]]) -> CarryOverOperation:
    return CarryOverOperation(
        connector_id=coords.connector_id,
        op_id=coords.op_id,
        step_of=list(steps.get(coords.op_id, [])),
    )


def _warnings(operations: Iterable[CarryOverOperation], *, sandbox: bool) -> list[str]:
    texts = [_step_text(op.op_id, op.step_of) for op in operations if op.step_of]
    if sandbox:
        texts.append(_SANDBOX_TEXT)
    texts.append(_PREVIEW_TEXT)
    return texts


async def _override_settings(
    session: AsyncSession,
    scope: Scope,
    tenant_id: UUID,
    steps: Mapping[str, list[str]],
    *,
    sandbox: bool,
) -> list[CarryOverSetting]:
    stmt = (
        select(OperationPolicyOverride)
        .where(
            _scope_filter(OperationPolicyOverride.tenant_id, scope, tenant_id),
            OperationPolicyOverride.requires_approval.is_(True),
        )
        .order_by(
            OperationPolicyOverride.impl_id,
            OperationPolicyOverride.version,
            OperationPolicyOverride.op_id,
        )
    )
    settings: list[CarryOverSetting] = []
    for row in (await session.execute(stmt)).scalars():
        connector_id = f"{row.impl_id}-{row.version}"
        operation = _operation(OpCoordinates(connector_id, None, row.op_id), steps)
        settings.append(
            CarryOverSetting(
                source="override",
                scope=scope,
                ref=f"{scope}:override",
                match={"connector": connector_id, "op": row.op_id},
                operations=[operation],
                warnings=_warnings([operation], sandbox=sandbox),
            )
        )
    return settings


async def _stored_entries(
    session: AsyncSession, scope: Scope, tenant_id: UUID
) -> list[ClassificationEntry]:
    stmt = select(TenantPolicy.document).where(
        _scope_filter(TenantPolicy.tenant_id, scope, tenant_id)
    )
    document = (await session.execute(stmt)).scalar_one_or_none()
    try:
        return entries_from_document(document, scope=scope)
    except ValueError as exc:
        raise InvalidStoredPolicyError(
            f"The stored policy of scope {scope} is not valid: {exc} Import a fixed policy file."
        ) from exc


async def _file_settings(
    session: AsyncSession,
    scope: Scope,
    tenant_id: UUID,
    steps: Mapping[str, list[str]],
    *,
    sandbox: bool,
) -> list[CarryOverSetting]:
    entries = await _stored_entries(session, scope, tenant_id)
    waits = [e for e in entries if e.always_wait]
    if not waits:
        return []
    reached: dict[str, list[CarryOverOperation]] = {e.ref: [] for e in waits}
    for coords in await _operations(session, scope, tenant_id):
        operation = _operation(coords, steps)
        for entry in _waiting_entries(entries, coords):
            # One operation can have two stored rows with different groups.
            if operation not in reached[entry.ref]:
                reached[entry.ref].append(operation)
    settings: list[CarryOverSetting] = []
    for entry in waits:
        operations = reached[entry.ref]
        warnings = _warnings(operations, sandbox=sandbox)
        if not operations:
            warnings.insert(0, _NO_OPERATION_TEXT)
        match = {
            key: value
            for key, value in (
                ("connector", entry.connector),
                ("group", entry.group),
                ("op", entry.op),
            )
            if value is not None
        }
        settings.append(
            CarryOverSetting(
                source="policy_file",
                scope=scope,
                ref=entry.ref,
                match=match,
                operations=operations,
                warnings=warnings,
            )
        )
    return settings


async def carry_over_dry_run(
    session: AsyncSession, *, scope: Scope, tenant_id: UUID
) -> CarryOverDryRun:
    """List every saved "always wait" of *scope* and what the carry-over means for it.

    *scope* ``tenant`` lists the settings of *tenant_id*; ``install`` the
    settings for the whole install. The caller checks who may read it.
    Reads only: no row and no answer changes.

    Raises :class:`~meho_backplane.policy.store.InvalidStoredPolicyError`
    when the stored policy document of *scope* is not valid.
    """
    sandbox = sandbox_mode_active()
    steps = _steps_by_op()
    settings = [
        *await _override_settings(session, scope, tenant_id, steps, sandbox=sandbox),
        *await _file_settings(session, scope, tenant_id, steps, sandbox=sandbox),
    ]
    return CarryOverDryRun(scope=scope, sandbox_mode=sandbox, settings=settings)
