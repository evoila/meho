# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for out-of-process audit parent-linkage (#3028).

Covers the two acceptance criteria of the task directly:

1. *A multi-dispatch external run replays as one audit subtree spanning
   orchestration + resulting dispatches* — :func:`test_run_replays_as_one_subtree`.
2. *Linkage only accepted from the paired principal for its own work_refs* —
   :func:`test_non_service_principal_gets_no_linkage`,
   :func:`test_unpaired_service_principal_gets_no_linkage`, and
   :func:`test_distinct_principals_same_work_ref_are_isolated`.

Plus the resolve-or-open idempotency the multi-dispatch grouping relies on and
the contextvar-binding contract :func:`write_audit_row` reads.

No Keycloak is touched: the pairing row is seeded straight into the DB (the
linkage seam only *reads* ``addon_pairing`` by clientId).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from meho_backplane.audit_query.replay import replay_session
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AddonOrchestrationRun,
    AddonPairing,
    AuditLog,
    Tenant,
)
from meho_backplane.operations._audit import agent_session_id_var, parent_audit_id_var
from meho_backplane.operations.addon_orchestration import (
    ORCHESTRATION_METHOD,
    ORCHESTRATION_PATH,
    bound_parent_linkage,
    resolve_or_open_orchestration_run,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OTHER_TENANT = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _seed_tenant(tenant_id: uuid.UUID = _TENANT, slug: str = "tenant-a") -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=slug.title()))
            await session.commit()


async def _seed_pairing(
    *,
    tenant_id: uuid.UUID = _TENANT,
    name: str = "automation",
    client_id: str = "addon:automation",
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AddonPairing(
                tenant_id=tenant_id,
                name=name,
                keycloak_client_id=client_id,
                keycloak_internal_id=str(uuid.uuid4()),
                owner_sub="human-owner",
                contract_version=1,
                addon_contract_version=1,
                addon_min_backplane_version=1,
                created_by_sub="human-operator",
            )
        )
        await session.commit()


def _service_operator(
    *,
    client_id: str | None = "addon:automation",
    tenant_id: uuid.UUID = _TENANT,
    sub: str = "svc-automation",
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.READ_ONLY,
        principal_kind=PrincipalKind.SERVICE,
        client_id=client_id,
    )


async def _count(model: type, **filters: object) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(func.count()).select_from(model)
        for col, val in filters.items():
            stmt = stmt.where(getattr(model, col) == val)
        return int((await session.execute(stmt)).scalar_one())


async def _insert_dispatch_row(op_id: str) -> None:
    """Insert one DISPATCH audit row exactly as ``write_audit_row`` would build it.

    Reads the lineage contextvars — so the test proves that a dispatch running
    *inside* :func:`bound_parent_linkage` inherits the run's session + parent.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                operator_sub="svc-automation",
                tenant_id=_TENANT,
                parent_audit_id=parent_audit_id_var.get(),
                agent_session_id=agent_session_id_var.get(),
                method="DISPATCH",
                path=op_id,
                status_code=200,
                duration_ms=Decimal("1.00"),
                payload={"op_id": op_id},
            )
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# Authorization: linkage only from a paired principal (criterion 2)
# --------------------------------------------------------------------------- #


async def test_non_service_principal_gets_no_linkage() -> None:
    await _seed_tenant()
    await _seed_pairing()
    user = Operator(
        sub="human",
        raw_jwt="",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        client_id=None,
    )
    assert await resolve_or_open_orchestration_run(user, "jira:OPS-1") is None
    assert await _count(AddonOrchestrationRun) == 0


async def test_unpaired_service_principal_gets_no_linkage() -> None:
    await _seed_tenant()
    # No pairing row seeded for this clientId.
    op = _service_operator(client_id="addon:not-paired")
    assert await resolve_or_open_orchestration_run(op, "jira:OPS-1") is None
    assert await _count(AddonOrchestrationRun) == 0


async def test_service_principal_without_client_id_gets_no_linkage() -> None:
    await _seed_tenant()
    await _seed_pairing()
    op = _service_operator(client_id=None)
    assert await resolve_or_open_orchestration_run(op, "jira:OPS-1") is None
    assert await _count(AddonOrchestrationRun) == 0


async def test_cross_tenant_pairing_is_declined() -> None:
    # A clientId whose pairing belongs to another tenant must never link.
    await _seed_tenant(tenant_id=_OTHER_TENANT, slug="tenant-b")
    await _seed_pairing(tenant_id=_OTHER_TENANT, client_id="addon:automation")
    await _seed_tenant()
    op = _service_operator(tenant_id=_TENANT, client_id="addon:automation")
    assert await resolve_or_open_orchestration_run(op, "jira:OPS-1") is None
    assert await _count(AddonOrchestrationRun) == 0


async def test_distinct_principals_same_work_ref_are_isolated() -> None:
    await _seed_tenant()
    await _seed_pairing(name="automation", client_id="addon:automation")
    await _seed_pairing(name="ssp", client_id="addon:ssp")
    op_a = _service_operator(client_id="addon:automation", sub="svc-a")
    op_b = _service_operator(client_id="addon:ssp", sub="svc-b")

    run_a = await resolve_or_open_orchestration_run(op_a, "jira:OPS-42")
    run_b = await resolve_or_open_orchestration_run(op_b, "jira:OPS-42")

    assert run_a is not None and run_b is not None
    # Same work_ref string, different principals → separate subtrees.
    assert run_a.session_id != run_b.session_id
    assert run_a.anchor_audit_id != run_b.anchor_audit_id
    assert await _count(AddonOrchestrationRun) == 2


# --------------------------------------------------------------------------- #
# Resolve-or-open idempotency (the multi-dispatch grouping foundation)
# --------------------------------------------------------------------------- #


async def test_open_writes_a_single_anchor_row() -> None:
    await _seed_tenant()
    await _seed_pairing()
    op = _service_operator()

    run = await resolve_or_open_orchestration_run(op, "jira:OPS-7")
    assert run is not None

    # Exactly one run row and one ORCHESTRATION-root audit row.
    assert await _count(AddonOrchestrationRun) == 1
    assert await _count(AuditLog, method=ORCHESTRATION_METHOD) == 1

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        anchor = (
            await session.execute(select(AuditLog).where(AuditLog.id == run.anchor_audit_id))
        ).scalar_one()
    assert anchor.method == ORCHESTRATION_METHOD
    assert anchor.path == ORCHESTRATION_PATH
    assert anchor.parent_audit_id is None
    assert anchor.agent_session_id == run.session_id
    assert anchor.work_ref == "jira:OPS-7"


async def test_resolve_is_idempotent_across_dispatches() -> None:
    await _seed_tenant()
    await _seed_pairing()
    op = _service_operator()

    first = await resolve_or_open_orchestration_run(op, "jira:OPS-7")
    second = await resolve_or_open_orchestration_run(op, "jira:OPS-7")

    assert first is not None and second is not None
    assert first.session_id == second.session_id
    assert first.anchor_audit_id == second.anchor_audit_id
    # Resolving the second time opens nothing new.
    assert await _count(AddonOrchestrationRun) == 1
    assert await _count(AuditLog, method=ORCHESTRATION_METHOD) == 1


# --------------------------------------------------------------------------- #
# Contextvar binding contract
# --------------------------------------------------------------------------- #


async def test_bound_parent_linkage_sets_and_resets_vars() -> None:
    await _seed_tenant()
    await _seed_pairing()
    op = _service_operator()
    run = await resolve_or_open_orchestration_run(op, "jira:OPS-7")
    assert run is not None

    assert agent_session_id_var.get() is None
    assert parent_audit_id_var.get() is None
    async with bound_parent_linkage(run):
        assert agent_session_id_var.get() == run.session_id
        assert parent_audit_id_var.get() == run.anchor_audit_id
    # Cleanly reset — the binding never leaks past the dispatch.
    assert agent_session_id_var.get() is None
    assert parent_audit_id_var.get() is None


# --------------------------------------------------------------------------- #
# Criterion 1: multi-dispatch run replays as ONE subtree
# --------------------------------------------------------------------------- #


async def test_run_replays_as_one_subtree() -> None:
    await _seed_tenant()
    await _seed_pairing()
    op = _service_operator()

    # First dispatch opens the run; two dispatches execute under its linkage.
    run = await resolve_or_open_orchestration_run(op, "jira:OPS-99")
    assert run is not None
    async with bound_parent_linkage(run):
        await _insert_dispatch_row("vmware-rest-9.0:vm.list")
    # A later dispatch resolves the SAME run (out-of-process, fresh context).
    run2 = await resolve_or_open_orchestration_run(op, "jira:OPS-99")
    assert run2 is not None
    async with bound_parent_linkage(run2):
        await _insert_dispatch_row("vmware-rest-9.0:vm.power_on")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        roots = await replay_session(run.session_id, tenant_id=_TENANT, session=session)

    # One subtree: the orchestration anchor is the sole root; both dispatches
    # nest under it.
    assert len(roots) == 1
    anchor = roots[0]
    assert anchor.method == ORCHESTRATION_METHOD
    assert anchor.path == ORCHESTRATION_PATH
    assert len(anchor.children) == 2
    child_paths = {child.path for child in anchor.children}
    assert child_paths == {"vmware-rest-9.0:vm.list", "vmware-rest-9.0:vm.power_on"}
    for child in anchor.children:
        assert child.method == "DISPATCH"
        assert child.agent_session_id == run.session_id
