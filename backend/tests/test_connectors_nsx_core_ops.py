# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the NSX read-only v0.2 core-ops curation.

Covers :mod:`meho_backplane.connectors.nsx.core_ops`:

* :func:`classify_nsx_op` — path-prefix classifier rules.
* :func:`apply_nsx_core_curation` — the operator-review-time
  substrate call that flips ``review_status='enabled'`` on the 8
  curated groups, lands ``llm_instructions`` on the 9 curated ops,
  and explicitly disables non-core ops in curated groups via the
  audit-log-driven operator-override exclusion so the
  :meth:`enable_group` cascade skips them. The load-bearing
  assertion is "9 ops dispatchable, every other op in curated
  groups stays ``is_enabled=False``".

Test harness mirrors :mod:`tests.test_operations_ingest_review`:
SQLite via the autouse ``_default_database_url`` fixture, hand-rolled
operator + connector seeding, audit-row counting from the chassis
``audit_log`` table.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.nsx import (
    NSX_CORE_GROUPS,
    NSX_CORE_OPS,
    NSX_IMPL_ID,
    NSX_PRODUCT,
    NSX_VERSION,
    apply_nsx_core_curation,
    classify_nsx_op,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction reads transitively.

    Mirrors :mod:`tests.test_operations_ingest_review` — the chassis
    :func:`get_settings` is cached, so each test rotates the cache
    before and after to keep cross-test leakage out.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_FAKE_JWT = "header.payload.signature"

#: Stable tenant id for the curation tests. ``None`` would be
#: closer to the production NSX shape (built-in scope) but tests
#: that exercise the audit-row + cascade path are easier to keep
#: isolated under a fresh tenant UUID per test.
_TEST_TENANT: uuid.UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    """Build a tenant-admin :class:`Operator` for the curation tests."""
    return Operator(
        sub=f"nsx-core-ops-test-{uuid.uuid4()}",
        name="NSX Core Ops Test",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed_curated_groups_and_ops(
    *,
    tenant_id: uuid.UUID,
    extra_ops_per_group: int = 0,
) -> dict[str, uuid.UUID]:
    """Seed every :data:`NSX_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``, name + when_to_use placeholders
    so :func:`apply_nsx_core_curation`'s ``edit_group`` step has
    something to overwrite). For each :data:`NSX_CORE_OPS` entry,
    inserts the curated op (``is_enabled=False`` to match the
    G0.7 ingest default for ``source_kind='ingested'`` rows).

    When *extra_ops_per_group* > 0, also seeds *N* non-curated
    ops per curated group with deterministic op_ids
    (``f"GET:/canary-extra/{group_key}/{i}"``) so the test can
    assert :func:`apply_nsx_core_curation` correctly disables
    them.

    Returns ``{group_key: group_id}`` for follow-up assertions.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}
    async with sessionmaker() as session:
        for group in NSX_CORE_GROUPS:
            group_id = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=NSX_PRODUCT,
                    version=NSX_VERSION,
                    impl_id=NSX_IMPL_ID,
                    group_key=group.group_key,
                    name=f"PLACEHOLDER NAME for {group.group_key}",
                    when_to_use=f"PLACEHOLDER when_to_use for {group.group_key}",
                    review_status="staged",
                ),
            )
            group_ids[group.group_key] = group_id

        for op in NSX_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=NSX_PRODUCT,
                    version=NSX_VERSION,
                    impl_id=NSX_IMPL_ID,
                    op_id=op.op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    group_id=group_ids[op.group_key],
                    summary=f"ingested op {op.op_id}",
                    is_enabled=False,
                ),
            )

        if extra_ops_per_group:
            for group in NSX_CORE_GROUPS:
                for i in range(extra_ops_per_group):
                    extra_op_id = f"GET:/canary-extra/{group.group_key}/{i}"
                    session.add(
                        EndpointDescriptor(
                            tenant_id=tenant_id,
                            product=NSX_PRODUCT,
                            version=NSX_VERSION,
                            impl_id=NSX_IMPL_ID,
                            op_id=extra_op_id,
                            source_kind="ingested",
                            method="GET",
                            path=f"/canary-extra/{group.group_key}/{i}",
                            group_id=group_ids[group.group_key],
                            summary=f"non-core op in {group.group_key}",
                            is_enabled=False,
                        ),
                    )
        await session.commit()
    return group_ids


async def _ops_state(
    *,
    tenant_id: uuid.UUID,
) -> dict[str, dict[str, Any]]:
    """Return ``{op_id: {is_enabled, llm_instructions}}`` for every NSX op."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.product == NSX_PRODUCT,
            EndpointDescriptor.version == NSX_VERSION,
            EndpointDescriptor.impl_id == NSX_IMPL_ID,
        )
        rows = (await session.execute(stmt)).scalars().all()
    return {
        row.op_id: {
            "is_enabled": row.is_enabled,
            "llm_instructions": row.llm_instructions,
        }
        for row in rows
    }


async def _group_statuses(*, tenant_id: uuid.UUID) -> dict[str, str]:
    """Return ``{group_key: review_status}`` for every NSX group."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.tenant_id == tenant_id,
            OperationGroup.product == NSX_PRODUCT,
            OperationGroup.version == NSX_VERSION,
            OperationGroup.impl_id == NSX_IMPL_ID,
        )
        rows = (await session.execute(stmt)).scalars().all()
    return {row.group_key: row.review_status for row in rows}


# ---------------------------------------------------------------------------
# classify_nsx_op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected",
    [
        ("GET:/api/v1/node", "manager-node"),
        ("GET:/api/v1/transport-nodes", "manager-transport-nodes"),
        ("GET:/api/v1/cluster/status", "manager-cluster"),
        ("GET:/policy/api/v1/infra/segments", "policy-segments"),
        ("GET:/policy/api/v1/infra/tier-0s", "policy-tier0"),
        ("GET:/policy/api/v1/infra/tier-1s", "policy-tier1"),
        (
            "GET:/policy/api/v1/infra/domains/default/security-policies",
            "policy-firewall",
        ),
        ("GET:/some/uncategorised/path", "none"),
        ("malformed_op_id_no_colon", "none"),
    ],
)
def test_classify_nsx_op_maps_paths_to_expected_group_keys(
    op_id: str,
    expected: str,
) -> None:
    """Path-prefix classifier returns the canonical ``group_key`` per path."""
    assert classify_nsx_op(op_id) == expected


# ---------------------------------------------------------------------------
# apply_nsx_core_curation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_curation_enables_groups_and_lands_llm_instructions() -> None:
    """All 8 curated groups end ``enabled``; all 9 core ops carry the curated blob."""
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_nsx_core_curation(ReviewService(operator), tenant_id=tenant_id)

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert all(s == "enabled" for s in statuses.values()), statuses
    assert set(statuses) == {g.group_key for g in NSX_CORE_GROUPS}

    state = await _ops_state(tenant_id=tenant_id)
    for op in NSX_CORE_OPS:
        row = state[op.op_id]
        assert row["is_enabled"] is True, op.op_id
        assert row["llm_instructions"] == op.llm_instructions, op.op_id


@pytest.mark.asyncio
async def test_apply_curation_keeps_non_core_ops_disabled_via_override_path() -> None:
    """Non-core ops in curated groups stay ``is_enabled=False`` after enable_group cascade.

    The audit-log-driven operator-override exclusion (see
    :func:`meho_backplane.operations.ingest._internals.operator_disabled_op_ids`)
    is the substrate that makes per-op exclusivity work inside a
    group-level enable. This test seeds 2 extra non-core ops per
    curated group, runs the helper, and asserts only the 9 curated
    ops flip to ``is_enabled=True`` while the 16 non-core ops
    (8 groups times 2 extras) stay ``False``.
    """
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_nsx_core_curation(ReviewService(operator), tenant_id=tenant_id)

    state = await _ops_state(tenant_id=tenant_id)
    core_op_ids = {op.op_id for op in NSX_CORE_OPS}
    for op_id, row in state.items():
        if op_id in core_op_ids:
            assert row["is_enabled"] is True, (
                f"curated core op {op_id} should be enabled after curation"
            )
        else:
            assert row["is_enabled"] is False, (
                f"non-core op {op_id} should stay disabled after curation; "
                f"the operator-override path was the safeguard"
            )


@pytest.mark.asyncio
async def test_apply_curation_writes_expected_audit_rows() -> None:
    """The curation step emits the expected audit-row breakdown.

    Per group: 1 ``meho.connector.edit_group`` row (always) + at
    most 1 ``meho.connector.enable_group`` row (suppressed if the
    group was already enabled). Per non-core op in a curated group:
    1 ``meho.connector.edit_op`` row carrying
    ``is_enabled_set_to=False``. Per core op: 1
    ``meho.connector.edit_op`` row carrying
    ``fields_updated=['llm_instructions']``.

    The audit-row counts are the canonical proof the helper
    actually walked the audit path it claims to walk; a future
    refactor that silently bypasses ``ReviewService`` would fail
    this assertion.
    """
    tenant_id = _TEST_TENANT
    extras_per_group = 1
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=extras_per_group)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_nsx_core_curation(ReviewService(operator), tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()

    by_path: dict[str, list[AuditLog]] = {}
    for row in rows:
        by_path.setdefault(row.path, []).append(row)

    group_count = len(NSX_CORE_GROUPS)
    op_count = len(NSX_CORE_OPS)
    non_core_count = group_count * extras_per_group

    assert len(by_path.get("meho.connector.edit_group", [])) == group_count, (
        f"one edit_group row per curated group; got "
        f"{len(by_path.get('meho.connector.edit_group', []))}"
    )
    assert len(by_path.get("meho.connector.enable_group", [])) == group_count, (
        f"one enable_group row per curated group (none were enabled "
        f"before this run); got {len(by_path.get('meho.connector.enable_group', []))}"
    )
    # edit_op rows partition: ``non_core_count`` for the disable
    # pass + ``op_count`` for the llm_instructions pass.
    edit_op_rows = by_path.get("meho.connector.edit_op", [])
    assert len(edit_op_rows) == non_core_count + op_count, (
        f"expected {non_core_count + op_count} edit_op rows "
        f"({non_core_count} non-core disables + {op_count} curated "
        f"llm_instructions); got {len(edit_op_rows)}"
    )
    disable_rows = [r for r in edit_op_rows if r.payload.get("is_enabled_set_to") is False]
    assert len(disable_rows) == non_core_count, (
        f"expected {non_core_count} disable rows; got {len(disable_rows)}"
    )
    llm_rows = [r for r in edit_op_rows if r.payload.get("fields_updated") == ["llm_instructions"]]
    assert len(llm_rows) == op_count, (
        f"expected {op_count} llm_instructions rows; got {len(llm_rows)}"
    )


@pytest.mark.asyncio
async def test_apply_curation_is_safe_to_rerun() -> None:
    """Re-running the helper on an already-curated connector keeps state correct.

    The audit layer isn't idempotent (each ``edit_group`` /
    ``edit_op`` writes a fresh row even on no-op values), but the
    end-state assertions hold: 9 ops enabled with the curated
    blob; non-core ops still disabled; all curated groups still
    enabled.
    """
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=1)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_nsx_core_curation(ReviewService(operator), tenant_id=tenant_id)
    # Second pass — the substrate must not regress any state.
    await apply_nsx_core_curation(ReviewService(operator), tenant_id=tenant_id)

    state = await _ops_state(tenant_id=tenant_id)
    core_op_ids = {op.op_id for op in NSX_CORE_OPS}
    for op_id, row in state.items():
        if op_id in core_op_ids:
            assert row["is_enabled"] is True, op_id
        else:
            assert row["is_enabled"] is False, op_id
    statuses = await _group_statuses(tenant_id=tenant_id)
    assert all(s == "enabled" for s in statuses.values()), statuses
