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

The carry-over reads such a setting as a raise to ``destructive``.
:func:`carry_over_dry_run` lists every saved setting of one scope and
shows what changes for each setting if MEHO reads it as ``destructive``.
It only reads: it changes no row and no answer of the gate, so it reads
no switch. It names a connector with no version by its product
(``product_form=True`` of :func:`~meho_backplane.policy.store.connector_id_of`)
and lists each operation under every entry that makes it wait, in the
form with the dash or by the product.

Per setting it says:

* where the setting comes from (``override`` or ``policy_file``) and its
  scope (``tenant`` or ``install``);
* the operations it matches, and for each one every multi-step operation
  that runs it as a step, at every level
  (:mod:`~meho_backplane.operations.governed_subops`). A policy file
  entry is matched against the operations a call of the scope can use,
  and against every declared step with the group the step gate finds
  for it, also a step with no row a call can use. An entry names the
  multi-step operations of an operation only when it makes the step
  wait for at least one run of the scope, not only the direct call. As
  ``destructive``, a step of a multi-step operation is refused, not held
  for approval;
* whether the install is in sandbox mode. There a delete grant of a
  service login can cover some ``destructive`` operations;
* that, as ``destructive``, a call needs a matching preview first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationGroup,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.operations._code_presence import row_code_missing
from meho_backplane.operations._lookup import (
    enabled_for_tenant_clause,
    installed_owner_clause,
    parse_connector_id,
    visible_scope_clause,
)
from meho_backplane.operations.governed_subops import registered_governed_subop_surfaces
from meho_backplane.policy.classification import (
    ClassificationEntry,
    OpCoordinates,
    Scope,
    entries_from_document,
)
from meho_backplane.policy.store import (
    InvalidStoredPolicyError,
    StepGroups,
    connector_id_of,
    coordinates_of,
    load_step_groups,
)
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
    "As `destructive`: this install is in sandbox mode. There a delete grant of a service "
    "login can cover some `destructive` operations, but not every one."
)
_NO_OPERATION_TEXT: Final[str] = (
    "As `destructive`, this entry changes no operation: it matches none."
)

#: A step of a multi-step operation: the connector id and the op id.
_StepKey = tuple[str, str]


def _step_text(op_id: str, composites: Sequence[str]) -> str:
    return (
        "As `destructive`, a step of a multi-step operation is refused, not held for "
        f"approval. `{op_id}` is a step of {', '.join(f'`{c}`' for c in composites)}, "
        "so such a run stops at this step."
    )


class CarryOverOperation(BaseModel):
    """One operation a saved setting matches.

    ``step_of`` lists every multi-step operation that runs it as a step,
    at every level, each once. It is empty when none does, or when the
    setting makes only the direct call wait, not the step.
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
    ``op``). ``warnings`` say what changes if MEHO reads it as ``destructive``.
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
    """The stored operations a setting of *scope* can match, each once.

    A tenant's settings reach the operations a call of the tenant can use,
    with the checks of dispatch (:func:`~meho_backplane.operations._lookup.lookup_descriptor`):
    the rows the tenant sees (:func:`visible_scope_clause`, so with its own
    copy of a connector only its own rows count), turned on for the tenant
    (:func:`enabled_for_tenant_clause`), of an installed package
    (:func:`installed_owner_clause`), and, row by row, not of a connector
    whose code is not in this MEHO (:func:`row_code_missing`). So an
    imported operation stays, also next to a handler that is not here.
    The install's settings reach every operation.
    """
    group = aliased(OperationGroup)
    stmt = select(
        EndpointDescriptor.product,
        EndpointDescriptor.version,
        EndpointDescriptor.impl_id,
        EndpointDescriptor.source_kind,
        EndpointDescriptor.handler_ref,
        EndpointDescriptor.owner_package,
        group.group_key,
        EndpointDescriptor.op_id,
    ).outerjoin(group, EndpointDescriptor.group_id == group.id)
    if scope == "tenant":
        stmt = stmt.where(
            visible_scope_clause(
                EndpointDescriptor,
                tenant_id=tenant_id,
                product=EndpointDescriptor.product,
                version=EndpointDescriptor.version,
                impl_id=EndpointDescriptor.impl_id,
            ),
            enabled_for_tenant_clause(tenant_id=tenant_id),
            installed_owner_clause(),
        )
    found: set[OpCoordinates] = set()
    for product, version, impl_id, kind, ref, owner, group_key, op_id in (
        await session.execute(stmt)
    ).tuples():
        row = EndpointDescriptor(
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
            source_kind=kind,
            handler_ref=ref,
            owner_package=owner,
        )
        if scope == "tenant" and await row_code_missing(session, tenant_id=tenant_id, row=row):
            continue
        found.add(coordinates_of(row, group_key, product_form=True))
    return sorted(found, key=lambda c: (c.connector_id, c.op_id, c.group_key or ""))


def _steps_by_op() -> tuple[dict[_StepKey, list[str]], dict[_StepKey, EndpointDescriptor]]:
    """Map each step to every multi-step operation that runs it, at every level, sorted.

    A step is keyed by the connector of the list it is on and its op id.
    A step that is itself a multi-step operation has its own list, which
    is walked too. Each list is walked once per multi-step operation, so
    a list that loops back ends; a multi-step operation is never named
    as a step of itself.

    The second map holds, per step, the in-memory row the step gate builds
    for it (never stored), to find its group the same way.
    """
    surfaces = registered_governed_subop_surfaces()
    runs: dict[_StepKey, set[str]] = {}
    rows: dict[_StepKey, EndpointDescriptor] = {}
    for composite in surfaces:
        seen = {composite}
        pending = [composite]
        while pending:
            surface = surfaces.get(pending.pop(0))
            if surface is None:
                continue
            product, version, impl_id = parse_connector_id(surface.connector_id)
            for step in surface.sub_op_ids:
                row = EndpointDescriptor(
                    product=product, version=version, impl_id=impl_id, op_id=step
                )
                key = (connector_id_of(row, product_form=True), step)
                if step != composite:
                    runs.setdefault(key, set()).add(composite)
                    rows.setdefault(key, row)
                if step not in seen:
                    seen.add(step)
                    pending.append(step)
    return {key: sorted(composites) for key, composites in runs.items()}, rows


def _waiting_entries(
    entries: Sequence[ClassificationEntry], coords: OpCoordinates
) -> list[ClassificationEntry]:
    """The entries of one scope that make *coords* wait, in any form of its connector id.

    The most specific entries that set ``always_wait`` decide it; on a tie
    ``true`` wins. So an entry with ``true`` that a more specific
    ``always_wait: false`` overrules makes this operation not wait. Each
    form of the connector id (:meth:`OpCoordinates.forms`) is decided on
    its own, as the gate does, and an entry that makes either form wait is
    listed.
    """
    waiting: list[ClassificationEntry] = []
    for form in coords.forms():
        setting = [e for e in entries if e.always_wait is not None and e.matches(form)]
        if not setting:
            continue
        best = max(e.rank for e in setting)
        for entry in setting:
            if entry.rank == best and entry.always_wait and all(entry is not w for w in waiting):
                waiting.append(entry)
    return waiting


def _operation(coords: OpCoordinates, steps: Mapping[_StepKey, list[str]]) -> CarryOverOperation:
    return CarryOverOperation(
        connector_id=coords.connector_id,
        op_id=coords.op_id,
        step_of=list(steps.get((coords.connector_id, coords.op_id), [])),
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
    steps: Mapping[_StepKey, list[str]],
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
        connector_id = connector_id_of(row, product_form=True)
        operation = _operation(coordinates_of(row, None, product_form=True), steps)
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


def _step_coordinates(
    scope: Scope, tenant_id: UUID, row: EndpointDescriptor, groups: StepGroups
) -> list[OpCoordinates]:
    """A declared step once per group the step gate can find for it.

    The group of a run is the step gate's (:meth:`StepGroups.group_of`).
    A tenant's settings count for the runs of that tenant. The install's
    settings count for every run: the one of a tenant without its own row
    of the step and the one of each tenant with its own row.
    """
    runs: list[UUID | None] = [tenant_id]
    if scope == "install":
        runs = [None, *groups.tenants_with_own_row(row)]
    found = {groups.group_of(row, run) for run in runs}
    return [
        coordinates_of(row, group, product_form=True)
        for group in sorted(found, key=lambda g: g or "")
    ]


async def _file_settings(
    session: AsyncSession,
    scope: Scope,
    tenant_id: UUID,
    steps: Mapping[_StepKey, list[str]],
    step_rows: Mapping[_StepKey, EndpointDescriptor],
    *,
    sandbox: bool,
) -> list[CarryOverSetting]:
    entries = await _stored_entries(session, scope, tenant_id)
    waits = [e for e in entries if e.always_wait]
    if not waits:
        return []
    stored = await _operations(session, scope, tenant_id)
    groups = await load_step_groups(
        session, step_rows.values(), tenant_id=tenant_id, every_tenant=scope == "install"
    )
    as_steps = {
        key: _step_coordinates(scope, tenant_id, row, groups)
        for key, row in sorted(step_rows.items())
    }
    # The entries that make a step wait for at least one run: only they
    # name its multi-step operations.
    step_waits = {
        key: {e.ref for coords in runs for e in _waiting_entries(entries, coords)}
        for key, runs in as_steps.items()
    }
    candidates = sorted(
        {*stored, *(coords for runs in as_steps.values() for coords in runs)},
        key=lambda c: (c.connector_id, c.op_id, c.group_key or ""),
    )
    reached: dict[str, list[CarryOverOperation]] = {e.ref: [] for e in waits}
    listed: dict[str, set[_StepKey]] = {e.ref: set() for e in waits}
    for coords in candidates:
        key = (coords.connector_id, coords.op_id)
        for entry in _waiting_entries(entries, coords):
            # One operation can have two stored rows with different groups.
            if key not in listed[entry.ref]:
                listed[entry.ref].add(key)
                as_step = entry.ref in step_waits.get(key, set())
                reached[entry.ref].append(_operation(coords, steps if as_step else {}))
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
    steps, step_rows = _steps_by_op()
    settings = [
        *await _override_settings(session, scope, tenant_id, steps, sandbox=sandbox),
        *await _file_settings(session, scope, tenant_id, steps, step_rows, sandbox=sandbox),
    ]
    return CarryOverDryRun(scope=scope, sandbox_mode=sandbox, settings=settings)
