# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the vROps read-only v0.5 core-ops curation.

Covers :mod:`meho_backplane.connectors.vcf_operations.core_ops`:

* :func:`classify_vrops_op` — path classifier rules for vROps' flat
  ``/suite-api/api/...`` path structure. Validates that the
  ``/alertdefinitions`` rule precedes the ``/alerts`` rule (the only
  rule-ordering hazard in the vROps surface).
* :data:`VROPS_CORE_OPS` and :data:`VROPS_CORE_GROUPS` — every entry
  carries non-placeholder ``llm_instructions`` / ``when_to_use`` text
  (the AC's "non-empty reviewed text" gate).
* :func:`apply_vrops_core_curation` — the operator-review-time
  substrate call that flips ``review_status='enabled'`` on the 7
  curated groups, lands ``llm_instructions`` on the 8 curated ops,
  and explicitly disables non-core ops via the audit-log-driven
  operator-override exclusion. The load-bearing assertion is "8 ops
  dispatchable, every other op in curated groups stays
  ``is_enabled=False``".

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
from meho_backplane.connectors.vcf_operations import (
    VROPS_CORE_GROUPS,
    VROPS_CORE_OPS,
    VROPS_IMPL_ID,
    VROPS_PRODUCT,
    VROPS_VERSION,
    apply_vrops_core_curation,
    classify_vrops_op,
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


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    """Build a tenant-admin :class:`Operator` for the curation tests."""
    return Operator(
        sub=f"vrops-core-ops-test-{uuid.uuid4()}",
        name="vROps Core Ops Test",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# classify_vrops_op — path classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected_group",
    [
        ("GET:/suite-api/api/versions/current", "vrops-system"),
        ("GET:/suite-api/api/resources", "vrops-resources"),
        ("GET:/suite-api/api/resources/{id}", "vrops-resources"),
        ("GET:/suite-api/api/alerts", "vrops-alerts"),
        ("GET:/suite-api/api/alertdefinitions", "vrops-alert-definitions"),
        ("GET:/suite-api/api/symptoms", "vrops-symptoms"),
        ("GET:/suite-api/api/recommendations", "vrops-recommendations"),
        ("GET:/suite-api/api/supermetrics", "vrops-supermetrics"),
        # Non-curated paths → "none"
        ("GET:/suite-api/api/credentialkinds", "none"),
        ("GET:/suite-api/api/auth/sources", "none"),
        # Non-GET → "none"
        ("POST:/suite-api/api/resources", "none"),
        ("DELETE:/suite-api/api/alerts/{id}", "none"),
        # Malformed op_id → "none"
        ("bad-op-id-no-colon", "none"),
    ],
    ids=str,
)
def test_classify_vrops_op_returns_correct_group(op_id: str, expected_group: str) -> None:
    """classify_vrops_op returns the curated group_key for every core op."""
    assert classify_vrops_op(op_id) == expected_group


def test_classify_vrops_op_alertdefinitions_precedes_alerts_rule() -> None:
    """The ``/alertdefinitions`` path classifies as vrops-alert-definitions.

    Load-bearing rule-ordering: ``startswith("/suite-api/api/alerts")``
    would otherwise eat the longer ``/suite-api/api/alertdefinitions``
    path and misclassify it.
    """
    assert classify_vrops_op("GET:/suite-api/api/alertdefinitions") == "vrops-alert-definitions"
    # And the alerts path stays in vrops-alerts.
    assert classify_vrops_op("GET:/suite-api/api/alerts") == "vrops-alerts"


def test_classify_vrops_op_all_core_ops_are_classified() -> None:
    """Every op in VROPS_CORE_OPS classifies to its declared group_key."""
    for op in VROPS_CORE_OPS:
        group = classify_vrops_op(op.op_id)
        assert group != "none", (
            f"VROPS_CORE_OPS entry {op.op_id!r} classified as 'none' — "
            "check VROPS_PATH_RULES ordering"
        )
        assert group == op.group_key, (
            f"VROPS_CORE_OPS entry {op.op_id!r} classified as {group!r} "
            f"but declared group_key={op.group_key!r}"
        )


# ---------------------------------------------------------------------------
# Curation data invariants
# ---------------------------------------------------------------------------


def test_vrops_core_ops_count_is_eight_and_read_only() -> None:
    """VROPS_CORE_OPS ships exactly the 8 read-only core ops per #833."""
    assert len(VROPS_CORE_OPS) == 8, (
        f"expected exactly 8 vROps core ops per #833 AC; got {len(VROPS_CORE_OPS)}"
    )
    for op in VROPS_CORE_OPS:
        assert op.op_id.startswith("GET:"), (
            f"vROps core op {op.op_id!r} must be a GET (v0.5 read-only core)"
        )


def test_vrops_core_groups_count_is_seven_and_unique() -> None:
    """VROPS_CORE_GROUPS spans the 7 vROps groups the 8 ops fall into."""
    assert len(VROPS_CORE_GROUPS) == 7, (
        f"expected exactly 7 vROps core groups; got {len(VROPS_CORE_GROUPS)}"
    )
    group_keys = [g.group_key for g in VROPS_CORE_GROUPS]
    assert len(set(group_keys)) == len(group_keys), (
        f"VROPS_CORE_GROUPS has duplicate group_keys: {group_keys!r}"
    )
    # Every op's group_key must resolve to a real group.
    op_group_keys = {op.group_key for op in VROPS_CORE_OPS}
    assert op_group_keys.issubset(set(group_keys)), (
        f"VROPS_CORE_OPS reference unknown groups: {op_group_keys - set(group_keys)!r}"
    )


def test_vrops_core_ops_llm_instructions_all_populated() -> None:
    """Every op has non-empty, non-placeholder llm_instructions with canonical keys.

    Asserts the AC bar: "every enabled op carries reviewed
    `llm_instructions`" — surfaces a regression where a future edit
    accidentally drops one of the three required keys or leaves a
    placeholder string in place.
    """
    required_keys = {"when_to_call", "output_shape", "next_step"}
    for op in VROPS_CORE_OPS:
        assert op.llm_instructions, f"op {op.op_id!r} has empty llm_instructions"
        missing = required_keys - set(op.llm_instructions)
        assert not missing, f"op {op.op_id!r} llm_instructions missing keys: {missing}"
        for key in required_keys:
            val = op.llm_instructions[key]
            assert isinstance(val, str) and val.strip(), (
                f"op {op.op_id!r} llm_instructions[{key!r}] must be a non-empty string"
            )
            # Reject obvious placeholders to enforce "operator-reviewed".
            assert "placeholder" not in val.lower(), (
                f"op {op.op_id!r} llm_instructions[{key!r}] contains placeholder text"
            )
            assert "tbd" not in val.lower(), (
                f"op {op.op_id!r} llm_instructions[{key!r}] contains TBD"
            )


def test_vrops_core_groups_when_to_use_all_populated() -> None:
    """Every group has non-empty, non-placeholder when_to_use + name."""
    for group in VROPS_CORE_GROUPS:
        assert group.when_to_use and group.when_to_use.strip(), (
            f"group {group.group_key!r} has empty when_to_use"
        )
        assert group.name and group.name.strip(), f"group {group.group_key!r} has empty name"
        assert "placeholder" not in group.when_to_use.lower(), (
            f"group {group.group_key!r} when_to_use contains placeholder text"
        )
        assert "tbd" not in group.when_to_use.lower(), (
            f"group {group.group_key!r} when_to_use contains TBD"
        )


# ---------------------------------------------------------------------------
# apply_vrops_core_curation — substrate integration (SQLite)
# ---------------------------------------------------------------------------


async def _seed_curated_groups_and_ops(
    *,
    tenant_id: uuid.UUID,
    extra_ops_per_group: int = 0,
) -> dict[str, uuid.UUID]:
    """Seed every :data:`VROPS_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``). For each :data:`VROPS_CORE_OPS`
    entry, inserts the curated op (``is_enabled=False`` to match the
    G0.7 ingest default for ``source_kind='ingested'`` rows).

    When *extra_ops_per_group* > 0, also seeds *N* non-curated ops per
    curated group with deterministic op_ids so the test can assert
    :func:`apply_vrops_core_curation` correctly disables them.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}

    async with sessionmaker() as session:
        for group in VROPS_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=tenant_id,
                product=VROPS_PRODUCT,
                version=VROPS_VERSION,
                impl_id=VROPS_IMPL_ID,
                group_key=group.group_key,
                name=f"Placeholder name for {group.group_key}",
                when_to_use="Placeholder when_to_use.",
                review_status="staged",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in VROPS_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=VROPS_PRODUCT,
                    version=VROPS_VERSION,
                    impl_id=VROPS_IMPL_ID,
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
                    tags=["spec:vcf-operations-9.0/suite-api.yaml"],
                )
            )

        for group in VROPS_CORE_GROUPS:
            for i in range(extra_ops_per_group):
                extra_op_id = f"GET:/canary-extra/{group.group_key}/{i}"
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=VROPS_PRODUCT,
                        version=VROPS_VERSION,
                        impl_id=VROPS_IMPL_ID,
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


async def test_apply_vrops_core_curation_enables_exactly_8_ops() -> None:
    """apply_vrops_core_curation enables exactly the 8 core ops."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrops_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == VROPS_PRODUCT,
                EndpointDescriptor.version == VROPS_VERSION,
                EndpointDescriptor.impl_id == VROPS_IMPL_ID,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        enabled_ops = result.scalars().all()

    enabled_op_ids = {op.op_id for op in enabled_ops}
    expected_op_ids = {op.op_id for op in VROPS_CORE_OPS}
    assert enabled_op_ids == expected_op_ids, (
        f"enabled ops don't match VROPS_CORE_OPS; "
        f"extra={enabled_op_ids - expected_op_ids!r}, "
        f"missing={expected_op_ids - enabled_op_ids!r}"
    )


async def test_apply_vrops_core_curation_disables_non_core_ops_in_curated_groups() -> None:
    """Extra ops seeded in curated groups stay is_enabled=False after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)

    review_service = ReviewService(operator=operator)
    await apply_vrops_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == VROPS_PRODUCT,
                EndpointDescriptor.version == VROPS_VERSION,
                EndpointDescriptor.impl_id == VROPS_IMPL_ID,
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


async def test_apply_vrops_core_curation_sets_llm_instructions_on_all_core_ops() -> None:
    """llm_instructions is populated on all 8 core ops after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrops_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    expected_op_ids = {op.op_id for op in VROPS_CORE_OPS}
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == VROPS_PRODUCT,
                EndpointDescriptor.op_id.in_(list(expected_op_ids)),
            )
        )
        curated_ops = result.scalars().all()

    assert len(curated_ops) == len(VROPS_CORE_OPS)
    for op in curated_ops:
        assert op.llm_instructions is not None and op.llm_instructions, (
            f"llm_instructions not set on core op {op.op_id!r} after curation"
        )


async def test_apply_vrops_core_curation_enables_all_7_curated_groups() -> None:
    """All 7 curated groups land review_status='enabled' after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrops_core_curation(review_service, tenant_id=tenant_id)

    expected_group_keys = {g.group_key for g in VROPS_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == VROPS_PRODUCT,
                OperationGroup.group_key.in_(list(expected_group_keys)),
            )
        )
        groups = result.scalars().all()

    assert len(groups) == len(VROPS_CORE_GROUPS)
    for group in groups:
        assert group.review_status == "enabled", (
            f"group {group.group_key!r} should be 'enabled' after curation; "
            f"got review_status={group.review_status!r}"
        )


async def test_apply_vrops_core_curation_writes_operator_reviewed_when_to_use() -> None:
    """Curated groups carry the operator-reviewed when_to_use after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrops_core_curation(review_service, tenant_id=tenant_id)

    expected: dict[str, Any] = {g.group_key: g.when_to_use for g in VROPS_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == VROPS_PRODUCT,
            )
        )
        groups = result.scalars().all()

    for group in groups:
        if group.group_key in expected:
            assert group.when_to_use == expected[group.group_key], (
                f"group {group.group_key!r} when_to_use mismatch after curation"
            )


async def test_apply_vrops_core_curation_emits_audit_rows() -> None:
    """apply_vrops_core_curation writes at least one audit row per curated group."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrops_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
            )
        )
        audit_rows = result.scalars().all()

    assert len(audit_rows) >= len(VROPS_CORE_GROUPS), (
        f"expected ≥{len(VROPS_CORE_GROUPS)} audit rows from curation; got {len(audit_rows)}"
    )
