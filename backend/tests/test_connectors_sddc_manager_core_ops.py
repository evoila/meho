# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the SDDC Manager read-only v0.2 core-ops curation.

Covers :mod:`meho_backplane.connectors.sddc_manager.core_ops`:

* :func:`classify_sddc_op` — path-prefix classifier rules.
* :func:`apply_sddc_core_curation` — the operator-review-time
  substrate call that flips ``review_status='enabled'`` on the 8
  curated groups, lands ``llm_instructions`` on the 9 curated ops,
  and explicitly disables non-core ops in curated groups via the
  audit-log-driven operator-override exclusion so the
  :meth:`enable_group` cascade skips them. The load-bearing
  assertion is "9 ops dispatchable, every other op in curated
  groups stays ``is_enabled=False``".

Test harness mirrors :mod:`tests.test_connectors_nsx_core_ops`:
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
from meho_backplane.connectors.sddc_manager import (
    SDDC_CORE_GROUPS,
    SDDC_CORE_OPS,
    SDDC_IMPL_ID,
    SDDC_PRODUCT,
    SDDC_VERSION,
    apply_sddc_core_curation,
    classify_sddc_op,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction reads transitively.

    Mirrors :mod:`tests.test_connectors_nsx_core_ops` — the chassis
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

_TEST_TENANT: uuid.UUID = uuid.UUID("22222222-3333-4444-5555-666666666666")


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    """Build a tenant-admin :class:`Operator` for the curation tests."""
    return Operator(
        sub=f"sddc-core-ops-test-{uuid.uuid4()}",
        name="SDDC Core Ops Test",
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
    """Seed every :data:`SDDC_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``, placeholder name + when_to_use so
    :func:`apply_sddc_core_curation`'s ``edit_group`` step has something
    to overwrite). For each :data:`SDDC_CORE_OPS` entry, inserts the
    curated op (``is_enabled=False`` to match the G0.7 ingest default
    for ``source_kind='ingested'`` rows).

    When *extra_ops_per_group* > 0, also seeds *N* non-curated ops per
    curated group with deterministic op_ids
    (``f"GET:/canary-extra/{group_key}/{i}"``) so the test can assert
    :func:`apply_sddc_core_curation` correctly disables them.

    Returns ``{group_key: group_id}`` for follow-up assertions.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}
    async with sessionmaker() as session:
        for group in SDDC_CORE_GROUPS:
            group_id = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=SDDC_PRODUCT,
                    version=SDDC_VERSION,
                    impl_id=SDDC_IMPL_ID,
                    group_key=group.group_key,
                    name=f"PLACEHOLDER NAME for {group.group_key}",
                    when_to_use=f"PLACEHOLDER when_to_use for {group.group_key}",
                    review_status="staged",
                ),
            )
            group_ids[group.group_key] = group_id

        for op in SDDC_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=SDDC_PRODUCT,
                    version=SDDC_VERSION,
                    impl_id=SDDC_IMPL_ID,
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
            for group in SDDC_CORE_GROUPS:
                for i in range(extra_ops_per_group):
                    extra_op_id = f"GET:/canary-extra/{group.group_key}/{i}"
                    session.add(
                        EndpointDescriptor(
                            tenant_id=tenant_id,
                            product=SDDC_PRODUCT,
                            version=SDDC_VERSION,
                            impl_id=SDDC_IMPL_ID,
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
    """Return ``{op_id: {is_enabled, llm_instructions}}`` for every SDDC Manager op."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.product == SDDC_PRODUCT,
            EndpointDescriptor.version == SDDC_VERSION,
            EndpointDescriptor.impl_id == SDDC_IMPL_ID,
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
    """Return ``{group_key: review_status}`` for every SDDC Manager group."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.tenant_id == tenant_id,
            OperationGroup.product == SDDC_PRODUCT,
            OperationGroup.version == SDDC_VERSION,
            OperationGroup.impl_id == SDDC_IMPL_ID,
        )
        rows = (await session.execute(stmt)).scalars().all()
    return {row.group_key: row.review_status for row in rows}


# ---------------------------------------------------------------------------
# classify_sddc_op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected",
    [
        ("GET:/v1/releases/system", "sddc-releases"),
        ("GET:/v1/sddc-managers", "sddc-managers"),
        ("GET:/v1/domains", "sddc-domains"),
        ("GET:/v1/domains/some-uuid", "sddc-domains"),
        ("GET:/v1/clusters", "sddc-clusters"),
        ("GET:/v1/hosts", "sddc-hosts"),
        ("GET:/v1/network-pools", "sddc-network-pools"),
        ("GET:/v1/bundles", "sddc-bundles"),
        ("GET:/v1/tasks", "sddc-tasks"),
        ("GET:/v1/vcenters", "none"),
        ("GET:/v1/nsxt-clusters", "none"),
        ("GET:/v2/domains", "none"),
        ("malformed_op_id_no_colon", "none"),
    ],
)
def test_classify_sddc_op_maps_paths_to_expected_group_keys(
    op_id: str,
    expected: str,
) -> None:
    """Path-prefix classifier returns the canonical ``group_key`` per path."""
    assert classify_sddc_op(op_id) == expected


# ---------------------------------------------------------------------------
# apply_sddc_core_curation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_curation_enables_groups_and_lands_llm_instructions() -> None:
    """All 8 curated groups end ``enabled``; all 9 core ops carry the curated blob."""
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_sddc_core_curation(ReviewService(operator), tenant_id=tenant_id)

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert all(s == "enabled" for s in statuses.values()), statuses
    assert set(statuses) == {g.group_key for g in SDDC_CORE_GROUPS}

    state = await _ops_state(tenant_id=tenant_id)
    for op in SDDC_CORE_OPS:
        row = state[op.op_id]
        assert row["is_enabled"] is True, op.op_id
        assert row["llm_instructions"] == op.llm_instructions, op.op_id


@pytest.mark.asyncio
async def test_apply_curation_keeps_non_core_ops_disabled_via_override_path() -> None:
    """Non-core ops in curated groups stay ``is_enabled=False`` after enable_group cascade.

    Seeds 2 extra non-core ops per curated group, runs the helper, and
    asserts only the 9 curated ops flip to ``is_enabled=True`` while the
    16 non-core ops (8 groups times 2 extras) stay ``False``.
    """
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_sddc_core_curation(ReviewService(operator), tenant_id=tenant_id)

    state = await _ops_state(tenant_id=tenant_id)
    core_op_ids = {op.op_id for op in SDDC_CORE_OPS}
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

    Per group: 1 ``meho.connector.edit_group`` row + at most 1
    ``meho.connector.enable_group`` row (suppressed when already enabled).
    Per non-core op in a curated group: 1 ``meho.connector.edit_op`` row
    with ``is_enabled_set_to=False``. Per core op: 1
    ``meho.connector.edit_op`` row with
    ``fields_updated=['llm_instructions']``.
    """
    tenant_id = _TEST_TENANT
    extras_per_group = 1
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=extras_per_group)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_sddc_core_curation(ReviewService(operator), tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()

    by_path: dict[str, list[AuditLog]] = {}
    for row in rows:
        by_path.setdefault(row.path, []).append(row)

    group_count = len(SDDC_CORE_GROUPS)
    op_count = len(SDDC_CORE_OPS)
    non_core_count = group_count * extras_per_group

    assert len(by_path.get("meho.connector.edit_group", [])) == group_count, (
        f"one edit_group row per curated group; got "
        f"{len(by_path.get('meho.connector.edit_group', []))}"
    )
    assert len(by_path.get("meho.connector.enable_group", [])) == group_count, (
        f"one enable_group row per curated group; got "
        f"{len(by_path.get('meho.connector.enable_group', []))}"
    )
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
    """Re-running the helper on an already-curated connector keeps state correct."""
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=1)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_sddc_core_curation(ReviewService(operator), tenant_id=tenant_id)
    await apply_sddc_core_curation(ReviewService(operator), tenant_id=tenant_id)

    state = await _ops_state(tenant_id=tenant_id)
    core_op_ids = {op.op_id for op in SDDC_CORE_OPS}
    for op_id, row in state.items():
        if op_id in core_op_ids:
            assert row["is_enabled"] is True, op_id
        else:
            assert row["is_enabled"] is False, op_id
    statuses = await _group_statuses(tenant_id=tenant_id)
    assert all(s == "enabled" for s in statuses.values()), statuses
