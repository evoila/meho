# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Hetzner Robot read-only v0.2 core-ops curation.

Covers :mod:`meho_backplane.connectors.hetzner_robot.core_ops`:

* :func:`classify_robot_op` — path classifier rules for Robot's flat
  path structure. Validates that the most-specific prefix wins and that
  only GET verbs are classified as curated.
* :func:`apply_robot_core_curation` — the operator-review-time substrate
  call that flips ``review_status='enabled'`` on the 4 curated groups,
  lands ``llm_instructions`` on the 10 curated ops, and explicitly
  disables non-core ops via the audit-log-driven operator-override
  exclusion. The load-bearing assertion is "10 ops dispatchable, every
  other op in curated groups stays ``is_enabled=False``".
* ``llm_instructions`` completeness — every op has the three canonical
  keys with non-empty string values.
* ``when_to_use`` completeness — every group has a non-empty hint.

Test harness mirrors :mod:`tests.test_connectors_harbor_core_ops`:
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
from meho_backplane.connectors.hetzner_robot import (
    ROBOT_CORE_GROUPS,
    ROBOT_CORE_OPS,
    ROBOT_IMPL_ID,
    ROBOT_PRODUCT,
    ROBOT_VERSION,
    apply_robot_core_curation,
    classify_robot_op,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction reads transitively."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_FAKE_JWT = "header.payload.signature"

_TEST_TENANT: uuid.UUID = uuid.UUID("aaaabbbb-cccc-dddd-eeee-ffffffffffff")


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    """Build a tenant-admin :class:`Operator` for the curation tests."""
    return Operator(
        sub=f"robot-core-ops-test-{uuid.uuid4()}",
        name="Robot Core Ops Test",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# classify_robot_op — path classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected_group",
    [
        # About
        ("GET:/query", "robot-about"),
        # Servers
        ("GET:/server", "robot-servers"),
        ("GET:/server/{server-ip}", "robot-servers"),
        # Networking — all families
        ("GET:/ip", "robot-networking"),
        ("GET:/subnet", "robot-networking"),
        ("GET:/vswitch", "robot-networking"),
        ("GET:/vswitch/{id}", "robot-networking"),
        ("GET:/failover", "robot-networking"),
        ("GET:/rdns", "robot-networking"),
        # SSH keys
        ("GET:/key", "robot-ssh-keys"),
        # Non-curated paths → "none"
        ("GET:/boot", "none"),
        ("GET:/reset", "none"),
        ("GET:/wol", "none"),
        ("GET:/order/server/product", "none"),
        # Non-GET verbs → "none" (Robot write paths)
        ("POST:/key", "none"),
        ("DELETE:/key/{fingerprint}", "none"),
        ("POST:/server/{server-ip}/reset", "none"),
        # Malformed op_id → "none"
        ("bad-op-id-no-colon", "none"),
    ],
    ids=str,
)
def test_classify_robot_op_returns_correct_group(op_id: str, expected_group: str) -> None:
    """classify_robot_op returns the curated group_key for all 10 core ops."""
    assert classify_robot_op(op_id) == expected_group


def test_classify_robot_op_vswitch_info_classifies_as_networking() -> None:
    """GET:/vswitch/{id} classifies as robot-networking, not an artifact of /vswitch prefix."""
    assert classify_robot_op("GET:/vswitch/{id}") == "robot-networking"
    assert classify_robot_op("GET:/vswitch") == "robot-networking"


def test_classify_robot_op_all_core_ops_are_classified() -> None:
    """Every op in ROBOT_CORE_OPS classifies to a non-'none' group."""
    for op in ROBOT_CORE_OPS:
        group = classify_robot_op(op.op_id)
        assert group != "none", (
            f"ROBOT_CORE_OPS entry {op.op_id!r} classified as 'none' — "
            "check ROBOT_PATH_RULES ordering"
        )
        assert group == op.group_key, (
            f"ROBOT_CORE_OPS entry {op.op_id!r} classified as {group!r} "
            f"but declared group_key={op.group_key!r}"
        )


def test_classify_robot_op_all_core_ops_are_get() -> None:
    """Every op in ROBOT_CORE_OPS is a GET — v0.2 core is read-only."""
    for op in ROBOT_CORE_OPS:
        method, _ = op.op_id.split(":", 1)
        assert method == "GET", (
            f"ROBOT_CORE_OPS entry {op.op_id!r} is not GET — v0.2 core must be read-only"
        )


# ---------------------------------------------------------------------------
# llm_instructions and when_to_use completeness
# ---------------------------------------------------------------------------


def test_robot_core_ops_llm_instructions_all_populated() -> None:
    """Every op in ROBOT_CORE_OPS has non-empty llm_instructions with canonical keys."""
    required_keys = {"when_to_call", "output_shape", "next_step"}
    for op in ROBOT_CORE_OPS:
        assert op.llm_instructions, f"op {op.op_id!r} has empty llm_instructions"
        missing = required_keys - set(op.llm_instructions)
        assert not missing, f"op {op.op_id!r} llm_instructions missing keys: {missing}"
        for key in required_keys:
            val = op.llm_instructions[key]
            assert isinstance(val, str) and val.strip(), (
                f"op {op.op_id!r} llm_instructions[{key!r}] must be a non-empty string"
            )


def test_robot_core_groups_when_to_use_all_populated() -> None:
    """Every group in ROBOT_CORE_GROUPS has a non-empty when_to_use hint."""
    for group in ROBOT_CORE_GROUPS:
        assert group.when_to_use and group.when_to_use.strip(), (
            f"group {group.group_key!r} has empty when_to_use"
        )
        assert group.name and group.name.strip(), f"group {group.group_key!r} has empty name"


def test_robot_core_ops_count_is_10() -> None:
    """ROBOT_CORE_OPS contains exactly 10 ops per the G3.7 DoD."""
    assert len(ROBOT_CORE_OPS) == 10, (
        f"expected 10 ops in ROBOT_CORE_OPS; got {len(ROBOT_CORE_OPS)}"
    )


def test_robot_core_groups_count_is_4() -> None:
    """ROBOT_CORE_GROUPS contains exactly 4 groups."""
    assert len(ROBOT_CORE_GROUPS) == 4, (
        f"expected 4 groups in ROBOT_CORE_GROUPS; got {len(ROBOT_CORE_GROUPS)}"
    )


def test_robot_core_ops_all_have_unique_op_ids() -> None:
    """No two entries in ROBOT_CORE_OPS share an op_id."""
    op_ids = [op.op_id for op in ROBOT_CORE_OPS]
    assert len(op_ids) == len(set(op_ids)), (
        f"duplicate op_ids in ROBOT_CORE_OPS: {[x for x in op_ids if op_ids.count(x) > 1]}"
    )


def test_robot_core_groups_all_have_unique_group_keys() -> None:
    """No two entries in ROBOT_CORE_GROUPS share a group_key."""
    keys = [g.group_key for g in ROBOT_CORE_GROUPS]
    assert len(keys) == len(set(keys)), (
        f"duplicate group_keys in ROBOT_CORE_GROUPS: {[x for x in keys if keys.count(x) > 1]}"
    )


def test_robot_core_ops_group_keys_subset_of_core_groups() -> None:
    """Every op's group_key appears in ROBOT_CORE_GROUPS."""
    valid_keys = {g.group_key for g in ROBOT_CORE_GROUPS}
    for op in ROBOT_CORE_OPS:
        assert op.group_key in valid_keys, (
            f"op {op.op_id!r} has group_key={op.group_key!r} not in ROBOT_CORE_GROUPS"
        )


# ---------------------------------------------------------------------------
# apply_robot_core_curation — substrate integration (SQLite)
# ---------------------------------------------------------------------------


async def _seed_curated_groups_and_ops(
    *,
    tenant_id: uuid.UUID,
    extra_ops_per_group: int = 0,
) -> dict[str, uuid.UUID]:
    """Seed every :data:`ROBOT_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``). For each :data:`ROBOT_CORE_OPS` entry,
    inserts the curated op (``is_enabled=False`` to match the G0.7 ingest
    default for ``source_kind='ingested'`` rows).

    When *extra_ops_per_group* > 0, also seeds *N* non-curated ops per
    curated group with deterministic op_ids so the test can assert
    :func:`apply_robot_core_curation` correctly disables them.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}

    async with sessionmaker() as session:
        for group in ROBOT_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=tenant_id,
                product=ROBOT_PRODUCT,
                version=ROBOT_VERSION,
                impl_id=ROBOT_IMPL_ID,
                group_key=group.group_key,
                name=f"Placeholder name for {group.group_key}",
                when_to_use="Placeholder when_to_use.",
                review_status="staged",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in ROBOT_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=ROBOT_PRODUCT,
                    version=ROBOT_VERSION,
                    impl_id=ROBOT_IMPL_ID,
                    op_id=op.op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    handler_ref=None,
                    group_id=group_ids[op.group_key],
                    summary=f"Placeholder summary for {op.op_id}.",
                    description=f"Placeholder description for {op.op_id}.",
                    parameter_schema={"type": "object", "properties": {}},
                    response_schema={"type": "object"},
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=False,
                    tags=["spec:hetzner-robot-2026-04/robot-api.yaml"],
                )
            )

        for group in ROBOT_CORE_GROUPS:
            for i in range(extra_ops_per_group):
                extra_op_id = f"GET:/canary-extra/{group.group_key}/{i}"
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=ROBOT_PRODUCT,
                        version=ROBOT_VERSION,
                        impl_id=ROBOT_IMPL_ID,
                        op_id=extra_op_id,
                        source_kind="ingested",
                        method="GET",
                        path=f"/canary-extra/{group.group_key}/{i}",
                        handler_ref=None,
                        group_id=group_ids[group.group_key],
                        summary="Extra non-curated op.",
                        description="Extra non-curated op.",
                        parameter_schema={"type": "object", "properties": {}},
                        response_schema={"type": "object"},
                        llm_instructions=None,
                        safety_level="safe",
                        requires_approval=False,
                        is_enabled=False,
                        tags=[],
                    )
                )

        await session.commit()

    return group_ids


async def test_apply_robot_core_curation_enables_exactly_10_ops() -> None:
    """apply_robot_core_curation enables exactly the 10 core ops."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_robot_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == ROBOT_PRODUCT,
                EndpointDescriptor.version == ROBOT_VERSION,
                EndpointDescriptor.impl_id == ROBOT_IMPL_ID,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        enabled_ops = result.scalars().all()

    enabled_op_ids = {op.op_id for op in enabled_ops}
    expected_op_ids = {op.op_id for op in ROBOT_CORE_OPS}
    assert enabled_op_ids == expected_op_ids, (
        f"enabled ops don't match ROBOT_CORE_OPS; "
        f"extra={enabled_op_ids - expected_op_ids!r}, "
        f"missing={expected_op_ids - enabled_op_ids!r}"
    )


async def test_apply_robot_core_curation_disables_non_core_ops_in_curated_groups() -> None:
    """Extra ops seeded in curated groups stay is_enabled=False after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)

    review_service = ReviewService(operator=operator)
    await apply_robot_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == ROBOT_PRODUCT,
                EndpointDescriptor.version == ROBOT_VERSION,
                EndpointDescriptor.impl_id == ROBOT_IMPL_ID,
                EndpointDescriptor.op_id.like("GET:/canary-extra/%"),
            )
        )
        extra_ops = result.scalars().all()

    assert extra_ops, "expected extra non-curated ops to exist"
    for op in extra_ops:
        assert op.is_enabled is False, (
            f"non-core op {op.op_id!r} should be disabled after curation; "
            f"got is_enabled={op.is_enabled!r}"
        )


async def test_apply_robot_core_curation_sets_llm_instructions_on_all_core_ops() -> None:
    """llm_instructions is populated on all 10 core ops after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_robot_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    expected_op_ids = {op.op_id for op in ROBOT_CORE_OPS}
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == ROBOT_PRODUCT,
                EndpointDescriptor.op_id.in_(list(expected_op_ids)),
            )
        )
        curated_ops = result.scalars().all()

    assert len(curated_ops) == len(ROBOT_CORE_OPS)
    for op in curated_ops:
        assert op.llm_instructions is not None and op.llm_instructions, (
            f"llm_instructions not set on core op {op.op_id!r} after curation"
        )


async def test_apply_robot_core_curation_enables_all_4_curated_groups() -> None:
    """All 4 curated groups land review_status='enabled' after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_robot_core_curation(review_service, tenant_id=tenant_id)

    expected_group_keys = {g.group_key for g in ROBOT_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == ROBOT_PRODUCT,
                OperationGroup.group_key.in_(list(expected_group_keys)),
            )
        )
        groups = result.scalars().all()

    assert len(groups) == len(ROBOT_CORE_GROUPS)
    for group in groups:
        assert group.review_status == "enabled", (
            f"group {group.group_key!r} should be 'enabled' after curation; "
            f"got review_status={group.review_status!r}"
        )


async def test_apply_robot_core_curation_writes_operator_reviewed_when_to_use() -> None:
    """Curated groups carry the operator-reviewed when_to_use after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_robot_core_curation(review_service, tenant_id=tenant_id)

    expected: dict[str, Any] = {g.group_key: g.when_to_use for g in ROBOT_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == ROBOT_PRODUCT,
            )
        )
        groups = result.scalars().all()

    for group in groups:
        if group.group_key in expected:
            assert group.when_to_use == expected[group.group_key], (
                f"group {group.group_key!r} when_to_use mismatch after curation"
            )


async def test_apply_robot_core_curation_emits_audit_rows() -> None:
    """apply_robot_core_curation writes at least one audit row per curated group."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_robot_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
            )
        )
        audit_rows = result.scalars().all()

    assert len(audit_rows) >= len(ROBOT_CORE_GROUPS), (
        f"expected ≥{len(ROBOT_CORE_GROUPS)} audit rows from curation; got {len(audit_rows)}"
    )
