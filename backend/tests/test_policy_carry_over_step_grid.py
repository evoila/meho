# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dry run and the step gate find the group of a step the same way.

The grid runs every cell of: 2 scopes x 11 settings x 9 sets of rows x 2
steps (396 cells). In every cell the dry run of the "always wait"
carry-over names the multi-step operation for a step exactly when the
step gate (:func:`enforce_subop_policy`) makes that step wait for at
least one run of the scope, in both directions. For a policy file
setting, :func:`classify_step` gives the same answer too. A run of the
tenant scope is tenant A's; a run of the install scope is tenant A's or
tenant B's.

On the dry run as it was before commit f14b5588b4 (the last fix round of
pull request 7075, its first version), this grid finds 8 differences, all
in the install scope.

The multi-step operation ``cowapi.composite.rebuild`` of ``cowapi-1.x``
has two steps, ``cowapi.vm.update`` and ``cowapi.disk.resize``. All names
and values are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationGroup,
    OperationPolicyOverride,
    TenantOperationSetting,
    TenantPolicy,
)
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.policy import store
from meho_backplane.policy.carry_over import carry_over_dry_run
from meho_backplane.policy.classification import Scope
from meho_backplane.policy.store import StepGroups, classify_step
from meho_backplane.settings import get_settings

from ._carry_over_helpers import CONNECTOR, OTHER_TENANT, TENANT, add_steps
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _settings_env,  # noqa: F401
)

COMPOSITE = "cowapi.composite.rebuild"
STEP_A = "cowapi.vm.update"
STEP_B = "cowapi.disk.resize"
STEPS = (STEP_A, STEP_B)
SCOPES: tuple[Scope, ...] = ("install", "tenant")
MISSING_HANDLER = "meho_backplane.no_such_module_for_tests:handler"


@pytest.fixture(autouse=True)
def classification_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------

#: A tenant's own row of a step: the tenant and its group (``None``: in no group).
OwnRow = tuple[uuid.UUID, str | None]


@dataclass(frozen=True)
class StepRows:
    """The stored rows of one step: the built-in row's group, and the tenants' own rows."""

    built_in: str | bool | None = False  # False: no built-in row; None: in no group
    own: tuple[OwnRow, ...] = ()
    off_for_tenant: bool = False
    code_missing: bool = False


ROW_SETS: dict[str, dict[str, StepRows]] = {
    "built-in rows": {STEP_A: StepRows("compute"), STEP_B: StepRows("storage")},
    "tenant row in another group": {
        STEP_A: StepRows("compute", own=((TENANT, "storage"),)),
        STEP_B: StepRows("storage"),
    },
    "tenant row in no group": {
        STEP_A: StepRows("compute", own=((TENANT, None),)),
        STEP_B: StepRows("storage", own=((TENANT, None),)),
    },
    "only a tenant row": {
        STEP_A: StepRows(own=((TENANT, "storage"),)),
        STEP_B: StepRows("storage"),
    },
    "no row": {STEP_A: StepRows(), STEP_B: StepRows("storage")},
    "two tenants": {
        STEP_A: StepRows("compute", own=((OTHER_TENANT, "storage"),)),
        STEP_B: StepRows("storage", own=((TENANT, "compute"),)),
    },
    "both tenants, groups differ": {
        STEP_A: StepRows("storage", own=((TENANT, "compute"), (OTHER_TENANT, None))),
        STEP_B: StepRows(None, own=((OTHER_TENANT, "compute"),)),
    },
    "turned off": {
        STEP_A: StepRows("compute", off_for_tenant=True),
        STEP_B: StepRows("storage"),
    },
    "handler code not here": {
        STEP_A: StepRows("compute", code_missing=True),
        STEP_B: StepRows("storage"),
    },
}


def _row(tenant: uuid.UUID | None, op_id: str, group_id: uuid.UUID | None) -> EndpointDescriptor:
    return EndpointDescriptor(
        tenant_id=tenant,
        product="cowapi",
        version="1.x",
        impl_id="cowapi",
        op_id=op_id,
        source_kind="ingested",
        safety_level="safe",
        requires_approval=False,
        parameter_schema={"type": "object", "properties": {}},
        group_id=group_id,
    )


async def _clear(session: AsyncSession) -> None:
    for model in (
        EndpointDescriptor,
        OperationGroup,
        OperationPolicyOverride,
        TenantOperationSetting,
        TenantPolicy,
    ):
        await session.execute(delete(model))


async def _store_rows(session: AsyncSession, rows: dict[str, StepRows]) -> None:
    groups: dict[str, uuid.UUID] = {}
    for key in ("compute", "storage"):
        group = OperationGroup(
            product="cowapi",
            version="1.x",
            impl_id="cowapi",
            group_key=key,
            name=key,
            when_to_use="Fixture group.",
        )
        session.add(group)
        await session.flush()
        groups[key] = group.id
    for op_id, step in rows.items():
        if step.built_in is not False:
            row = _row(None, op_id, groups[step.built_in] if step.built_in else None)
            if step.code_missing:
                row.source_kind = "typed"
                row.handler_ref = MISSING_HANDLER
            session.add(row)
        for tenant, group_key in step.own:
            session.add(_row(tenant, op_id, groups[group_key] if group_key else None))
        if step.off_for_tenant:
            session.add(
                TenantOperationSetting(
                    tenant_id=TENANT,
                    product="cowapi",
                    version="1.x",
                    impl_id="cowapi",
                    op_id=op_id,
                    enabled=False,
                    set_by_sub="admin-a",
                    set_at=datetime.now(UTC),
                )
            )


# ---------------------------------------------------------------------------
# The settings
# ---------------------------------------------------------------------------

_WAIT = True


def _entries(*entries: tuple[dict[str, str], bool]) -> list[dict[str, Any]]:
    return [{"match": match, "always_wait": wait} for match, wait in entries]


#: Each setting: the entries of the scope's policy file, and an override of STEP_A.
SETTINGS: dict[str, tuple[list[dict[str, Any]], bool | None]] = {
    "by connector": (_entries(({"connector": CONNECTOR}, _WAIT)), None),
    "by group compute": (_entries(({"group": "compute"}, _WAIT)), None),
    "by group storage": (_entries(({"group": "storage"}, _WAIT)), None),
    "by operation": (_entries(({"op": STEP_A}, _WAIT)), None),
    "by operation pattern": (_entries(({"op": "cowapi.*"}, _WAIT)), None),
    "connector, but not group compute": (
        _entries(({"connector": CONNECTOR}, _WAIT), ({"group": "compute"}, False)),
        None,
    ),
    "group storage, but not the operation": (
        _entries(({"group": "storage"}, _WAIT), ({"op": STEP_A}, False)),
        None,
    ),
    "connector, but not the other step": (
        _entries(({"connector": CONNECTOR}, _WAIT), ({"op": STEP_B}, False)),
        None,
    ),
    "override with approval on": ([], True),
    "override with approval off": ([], False),
    "nothing": ([], None),
}


async def _store_setting(session: AsyncSession, scope: Scope, name: str) -> None:
    entries, approval = SETTINGS[name]
    owner = None if scope == "install" else TENANT
    if entries:
        session.add(
            TenantPolicy(
                tenant_id=owner,
                document={"classifications": entries},
                set_by_sub="admin-a",
            )
        )
    if approval is not None:
        session.add(
            OperationPolicyOverride(
                tenant_id=owner,
                product="cowapi",
                version="1.x",
                impl_id="cowapi",
                op_id=STEP_A,
                requires_approval=approval,
                set_by_sub="admin-a",
            )
        )


# ---------------------------------------------------------------------------
# The three answers
# ---------------------------------------------------------------------------


def _operator(tenant: uuid.UUID) -> Operator:
    return Operator(
        sub="op-a",
        name="Operator A",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant,
        tenant_role=TenantRole.OPERATOR,
    )


def _step_row(op_id: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="cowapi",
        version="1.x",
        impl_id="cowapi",
        op_id=op_id,
        safety_level="safe",
        requires_approval=False,
    )


async def _gate_waits(tenant: uuid.UUID, op_id: str) -> bool:
    result = await enforce_subop_policy(
        operator=_operator(tenant),
        connector_id=CONNECTOR,
        op_id=op_id,
        safety_level="safe",
        requires_approval=False,
        target=None,
        params={},
    )
    if result is None:
        return False
    assert result.status == "awaiting_approval", result
    return True


async def _classified_waits(tenant: uuid.UUID, op_id: str) -> bool:
    async with get_sessionmaker()() as session:
        classified = await classify_step(session, tenant_id=tenant, step=_step_row(op_id))
    return classified.always_wait


async def _named_steps(scope: Scope) -> set[str]:
    """The steps for which the dry run names the multi-step operation."""
    async with get_sessionmaker()() as session:
        answer = await carry_over_dry_run(session, scope=scope, tenant_id=TENANT)
    return {
        op.op_id
        for setting in answer.settings
        for op in setting.operations
        if COMPOSITE in op.step_of
    }


def _runs(scope: Scope) -> tuple[uuid.UUID, ...]:
    return (TENANT, OTHER_TENANT) if scope == "install" else (TENANT,)


async def _differences(scope: Scope, setting: str, rows: str) -> list[str]:
    async with get_sessionmaker()() as session:
        await _clear(session)
        await _store_rows(session, ROW_SETS[rows])
        await _store_setting(session, scope, setting)
        await session.commit()
    named = await _named_steps(scope)
    differences: list[str] = []
    for op_id in STEPS:
        gate = [await _gate_waits(run, op_id) for run in _runs(scope)]
        cell = f"{scope} / {setting} / {rows} / {op_id}"
        if (op_id in named) != any(gate):
            differences.append(f"{cell}: dry run {op_id in named}, step gate {gate}")
        if SETTINGS[setting][0]:
            classified = [await _classified_waits(run, op_id) for run in _runs(scope)]
            if classified != gate:
                differences.append(f"{cell}: classify_step {classified}, step gate {gate}")
    return differences


@pytest.fixture(autouse=True)
def two_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    add_steps(monkeypatch, COMPOSITE, CONNECTOR, *STEPS)


@pytest.mark.asyncio
async def test_the_grid_has_enough_cells() -> None:
    assert len(SCOPES) * len(SETTINGS) * len(ROW_SETS) * len(STEPS) >= 396


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", SCOPES)
async def test_the_dry_run_names_a_step_exactly_when_the_step_gate_makes_it_wait(
    scope: Scope,
) -> None:
    differences: list[str] = []
    for setting in SETTINGS:
        for rows in ROW_SETS:
            differences.extend(await _differences(scope, setting, rows))

    assert differences == []


@pytest.mark.asyncio
async def test_the_grid_sees_a_step_that_waits_for_one_run_only() -> None:
    """A cell where only one of two runs waits: the grid can tell the runs apart."""
    async with get_sessionmaker()() as session:
        await _store_rows(session, ROW_SETS["two tenants"])
        await _store_setting(session, "install", "by group storage")
        await session.commit()

    assert await _named_steps("install") == {STEP_A, STEP_B}
    assert [await _gate_waits(run, STEP_A) for run in _runs("install")] == [False, True]


# ---------------------------------------------------------------------------
# One shared function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_follow_the_shared_function(monkeypatch: pytest.MonkeyPatch) -> None:
    async with get_sessionmaker()() as session:
        await _store_rows(session, ROW_SETS["built-in rows"])
        await _store_setting(session, "tenant", "nothing")
        session.add(
            TenantPolicy(
                tenant_id=TENANT,
                document=_policy_doc({"group": "made-up"}),
                set_by_sub="admin-a",
            )
        )
        await session.commit()
    assert await _named_steps("tenant") == set()
    assert await _gate_waits(TENANT, STEP_A) is False

    real = StepGroups.group_of

    def made_up(self: StepGroups, step: Any, tenant_id: uuid.UUID | None) -> str | None:
        return "made-up" if step.op_id == STEP_A else real(self, step, tenant_id)

    monkeypatch.setattr(store.StepGroups, "group_of", made_up)

    assert await _named_steps("tenant") == {STEP_A}
    assert await _classified_waits(TENANT, STEP_A) is True
    assert await _gate_waits(TENANT, STEP_A) is True
    assert await _gate_waits(TENANT, STEP_B) is False


def _policy_doc(match: dict[str, str]) -> dict[str, Any]:
    return {"classifications": [{"match": match, "always_wait": True}]}


# ---------------------------------------------------------------------------
# One query for the groups of all steps
# ---------------------------------------------------------------------------


async def _statements(scope: Scope, count: int, monkeypatch: pytest.MonkeyPatch) -> int:
    steps = [f"cowapi.step.{i:03d}" for i in range(count)]
    add_steps(monkeypatch, COMPOSITE, CONNECTOR, *steps)
    async with get_sessionmaker()() as session:
        await _clear(session)
        await _store_rows(
            session,
            {op_id: StepRows("compute", own=((TENANT, "storage"),)) for op_id in steps},
        )
        session.add(
            TenantPolicy(
                tenant_id=None if scope == "install" else TENANT,
                document=_policy_doc({"group": "storage"}),
                set_by_sub="admin-a",
            )
        )
        await session.commit()
    seen: list[str] = []

    def _count(*args: Any) -> None:
        seen.append(str(args[2]))

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        named = await _named_steps(scope)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert named == set(steps)
    return len(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", SCOPES)
async def test_one_dry_run_reads_the_same_number_of_statements_for_10_and_100_steps(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    ten = await _statements(scope, 10, monkeypatch)
    hundred = await _statements(scope, 100, monkeypatch)

    assert ten == hundred
