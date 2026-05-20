# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Harbor read-only v0.2 core-ops curation.

Covers :mod:`meho_backplane.connectors.harbor.core_ops`:

* :func:`classify_harbor_op` — path classifier rules for Harbor's
  hierarchical path structure. Validates that the most-specific prefix
  wins for nested artifact, repository, and project paths.
* Robot secret invariant — asserts that :data:`HARBOR_CORE_OPS` never
  includes a write op that would expose a robot secret, and that the
  fixture data in :data:`HARBOR_CANARY_ROBOTS` contains no ``secret``
  key at any level.
* :func:`apply_harbor_core_curation` — the operator-review-time
  substrate call that flips ``review_status='enabled'`` on the 5
  curated groups, lands ``llm_instructions`` on the 9 curated ops,
  and explicitly disables non-core ops via the audit-log-driven
  operator-override exclusion. The load-bearing assertion is "9 ops
  dispatchable, every other op in curated groups stays
  ``is_enabled=False``".

Test harness mirrors :mod:`tests.test_connectors_sddc_manager_core_ops`:
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
from meho_backplane.connectors.harbor import (
    HARBOR_CORE_GROUPS,
    HARBOR_CORE_OPS,
    HARBOR_IMPL_ID,
    HARBOR_PRODUCT,
    HARBOR_VERSION,
    apply_harbor_core_curation,
    classify_harbor_op,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.settings import get_settings
from tests.acceptance._harbor_canary_fixtures import HARBOR_CANARY_ROBOTS


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

_TEST_TENANT: uuid.UUID = uuid.UUID("33333333-4444-5555-6666-777777777777")


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    """Build a tenant-admin :class:`Operator` for the curation tests."""
    return Operator(
        sub=f"harbor-core-ops-test-{uuid.uuid4()}",
        name="Harbor Core Ops Test",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# classify_harbor_op — path classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected_group",
    [
        ("GET:/api/v2.0/systeminfo", "harbor-system"),
        ("GET:/api/v2.0/health", "harbor-system"),
        ("GET:/api/v2.0/robots", "harbor-robots"),
        ("GET:/api/v2.0/projects", "harbor-projects"),
        ("GET:/api/v2.0/projects/{project_name}", "harbor-projects"),
        (
            "GET:/api/v2.0/projects/{project_name}/repositories",
            "harbor-repositories",
        ),
        (
            "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}",
            "harbor-repositories",
        ),
        (
            "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts",
            "harbor-artifacts",
        ),
        (
            "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}",
            "harbor-artifacts",
        ),
        # Non-curated paths → "none"
        ("GET:/api/v2.0/users", "none"),
        ("GET:/api/v2.0/configurations", "none"),
        ("POST:/api/v2.0/robots", "none"),
        # Malformed op_id → "none"
        ("bad-op-id-no-colon", "none"),
    ],
    ids=str,
)
def test_classify_harbor_op_returns_correct_group(op_id: str, expected_group: str) -> None:
    """classify_harbor_op returns the curated group_key for all 9 core ops."""
    assert classify_harbor_op(op_id) == expected_group


def test_classify_harbor_op_artifacts_precedes_repositories_in_hierarchy() -> None:
    """Artifact paths classify as harbor-artifacts, not harbor-repositories."""
    artifact_list = "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts"
    artifact_info = (
        "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}"
    )
    assert classify_harbor_op(artifact_list) == "harbor-artifacts"
    assert classify_harbor_op(artifact_info) == "harbor-artifacts"


def test_classify_harbor_op_all_core_ops_are_classified() -> None:
    """Every op in HARBOR_CORE_OPS classifies to a non-'none' group."""
    for op in HARBOR_CORE_OPS:
        group = classify_harbor_op(op.op_id)
        assert group != "none", (
            f"HARBOR_CORE_OPS entry {op.op_id!r} classified as 'none' — "
            "check HARBOR_PATH_RULES ordering"
        )
        assert group == op.group_key, (
            f"HARBOR_CORE_OPS entry {op.op_id!r} classified as {group!r} "
            f"but declared group_key={op.group_key!r}"
        )


# ---------------------------------------------------------------------------
# Robot secret invariant
# ---------------------------------------------------------------------------


def test_harbor_robot_list_response_fixture_contains_no_secret_field() -> None:
    """HARBOR_CANARY_ROBOTS has no 'secret' key at any level.

    The Harbor 2.x API guarantees list responses never expose the robot
    secret (only the POST create response does). This test asserts the
    fixture data enforces the invariant — if a future fixture edit
    accidentally added a 'secret' key, this test would catch it before
    the acceptance suite runs.
    """
    for robot in HARBOR_CANARY_ROBOTS:
        assert "secret" not in robot, (
            f"robot fixture entry {robot.get('name')!r} contains 'secret' — "
            "Harbor list responses must never expose robot secrets"
        )
        for key, val in robot.items():
            if isinstance(val, dict):
                assert "secret" not in val, (
                    f"nested key {key!r} in robot {robot.get('name')!r} contains 'secret'"
                )


def test_harbor_core_ops_robot_list_is_read_only_get() -> None:
    """The only robot op in HARBOR_CORE_OPS is a GET (no POST/DELETE)."""
    robot_ops = [op for op in HARBOR_CORE_OPS if "robot" in op.op_id.lower()]
    assert len(robot_ops) == 1, (
        f"expected exactly 1 robot op in HARBOR_CORE_OPS; got {[op.op_id for op in robot_ops]}"
    )
    robot_op = robot_ops[0]
    assert robot_op.op_id.startswith("GET:"), f"robot op must be GET-only; got {robot_op.op_id!r}"


def test_harbor_core_ops_llm_instructions_all_populated() -> None:
    """Every op in HARBOR_CORE_OPS has non-empty llm_instructions with canonical keys."""
    required_keys = {"when_to_call", "output_shape", "next_step"}
    for op in HARBOR_CORE_OPS:
        assert op.llm_instructions, f"op {op.op_id!r} has empty llm_instructions"
        missing = required_keys - set(op.llm_instructions)
        assert not missing, f"op {op.op_id!r} llm_instructions missing keys: {missing}"
        for key in required_keys:
            val = op.llm_instructions[key]
            assert isinstance(val, str) and val.strip(), (
                f"op {op.op_id!r} llm_instructions[{key!r}] must be a non-empty string"
            )


def test_harbor_core_groups_when_to_use_all_populated() -> None:
    """Every group in HARBOR_CORE_GROUPS has a non-empty when_to_use hint."""
    for group in HARBOR_CORE_GROUPS:
        assert group.when_to_use and group.when_to_use.strip(), (
            f"group {group.group_key!r} has empty when_to_use"
        )
        assert group.name and group.name.strip(), f"group {group.group_key!r} has empty name"


# ---------------------------------------------------------------------------
# apply_harbor_core_curation — substrate integration (SQLite)
# ---------------------------------------------------------------------------


async def _seed_curated_groups_and_ops(
    *,
    tenant_id: uuid.UUID,
    extra_ops_per_group: int = 0,
) -> dict[str, uuid.UUID]:
    """Seed every :data:`HARBOR_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``). For each :data:`HARBOR_CORE_OPS` entry,
    inserts the curated op (``is_enabled=False`` to match the G0.7 ingest
    default for ``source_kind='ingested'`` rows).

    When *extra_ops_per_group* > 0, also seeds *N* non-curated ops per
    curated group with deterministic op_ids so the test can assert
    :func:`apply_harbor_core_curation` correctly disables them.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}

    async with sessionmaker() as session:
        for group in HARBOR_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=tenant_id,
                product=HARBOR_PRODUCT,
                version=HARBOR_VERSION,
                impl_id=HARBOR_IMPL_ID,
                group_key=group.group_key,
                name=f"Placeholder name for {group.group_key}",
                when_to_use="Placeholder when_to_use.",
                review_status="staged",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in HARBOR_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=HARBOR_PRODUCT,
                    version=HARBOR_VERSION,
                    impl_id=HARBOR_IMPL_ID,
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
                    tags=["spec:harbor-2.x/swagger.yaml"],
                )
            )

        for group in HARBOR_CORE_GROUPS:
            for i in range(extra_ops_per_group):
                extra_op_id = f"GET:/canary-extra/{group.group_key}/{i}"
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=HARBOR_PRODUCT,
                        version=HARBOR_VERSION,
                        impl_id=HARBOR_IMPL_ID,
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


async def test_apply_harbor_core_curation_enables_exactly_9_ops() -> None:
    """apply_harbor_core_curation enables exactly the 9 core ops."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_harbor_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == HARBOR_PRODUCT,
                EndpointDescriptor.version == HARBOR_VERSION,
                EndpointDescriptor.impl_id == HARBOR_IMPL_ID,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        enabled_ops = result.scalars().all()

    enabled_op_ids = {op.op_id for op in enabled_ops}
    expected_op_ids = {op.op_id for op in HARBOR_CORE_OPS}
    assert enabled_op_ids == expected_op_ids, (
        f"enabled ops don't match HARBOR_CORE_OPS; "
        f"extra={enabled_op_ids - expected_op_ids!r}, "
        f"missing={expected_op_ids - enabled_op_ids!r}"
    )


async def test_apply_harbor_core_curation_disables_non_core_ops_in_curated_groups() -> None:
    """Extra ops seeded in curated groups stay is_enabled=False after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)

    review_service = ReviewService(operator=operator)
    await apply_harbor_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == HARBOR_PRODUCT,
                EndpointDescriptor.version == HARBOR_VERSION,
                EndpointDescriptor.impl_id == HARBOR_IMPL_ID,
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


async def test_apply_harbor_core_curation_sets_llm_instructions_on_all_core_ops() -> None:
    """llm_instructions is populated on all 9 core ops after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_harbor_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    expected_op_ids = {op.op_id for op in HARBOR_CORE_OPS}
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == HARBOR_PRODUCT,
                EndpointDescriptor.op_id.in_(list(expected_op_ids)),
            )
        )
        curated_ops = result.scalars().all()

    assert len(curated_ops) == len(HARBOR_CORE_OPS)
    for op in curated_ops:
        assert op.llm_instructions is not None and op.llm_instructions, (
            f"llm_instructions not set on core op {op.op_id!r} after curation"
        )


async def test_apply_harbor_core_curation_enables_all_5_curated_groups() -> None:
    """All 5 curated groups land review_status='enabled' after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_harbor_core_curation(review_service, tenant_id=tenant_id)

    expected_group_keys = {g.group_key for g in HARBOR_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == HARBOR_PRODUCT,
                OperationGroup.group_key.in_(list(expected_group_keys)),
            )
        )
        groups = result.scalars().all()

    assert len(groups) == len(HARBOR_CORE_GROUPS)
    for group in groups:
        assert group.review_status == "enabled", (
            f"group {group.group_key!r} should be 'enabled' after curation; "
            f"got review_status={group.review_status!r}"
        )


async def test_apply_harbor_core_curation_writes_operator_reviewed_when_to_use() -> None:
    """Curated groups carry the operator-reviewed when_to_use after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_harbor_core_curation(review_service, tenant_id=tenant_id)

    expected: dict[str, Any] = {g.group_key: g.when_to_use for g in HARBOR_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == HARBOR_PRODUCT,
            )
        )
        groups = result.scalars().all()

    for group in groups:
        if group.group_key in expected:
            assert group.when_to_use == expected[group.group_key], (
                f"group {group.group_key!r} when_to_use mismatch after curation"
            )


async def test_apply_harbor_core_curation_emits_audit_rows() -> None:
    """apply_harbor_core_curation writes at least one audit row per curated group."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_harbor_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
            )
        )
        audit_rows = result.scalars().all()

    assert len(audit_rows) >= len(HARBOR_CORE_GROUPS), (
        f"expected ≥{len(HARBOR_CORE_GROUPS)} audit rows from curation; got {len(audit_rows)}"
    )
